"""对讲呼叫编排器（契约 v1.8r1 §5 + §四 B-2/B-3）。

职责
----
在既有低层设施（intercom_registry 透传会话 + connection_registry 下发）之上，
提供「用户态呼叫」状态机与 **pending 上线补偿下发**（B-2）：

- REST 状态机：calling → connected / ended / failed(reason)（契约 §5.4）
- B-2 pending：呼叫时任一端离线不拒绝，呼叫单挂起（TTL=30s），后台每 1s
  轮询在线态，双方一上线立即走低层建会话 + 下发 SetIntercom；TTL 超时
  → failed(peer_offline)，向发起端设备推 peer_offline 事件、向小程序推
  intercom.call 事件（经 event_bus，B-4）。
- 对账：connected 后若低层会话已消亡（设备侧挂断/12s 兜底拆除），
  worker 将呼叫单收敛为 ended，保证 /call/active 不吐僵尸呼叫。

红线合规
--------
- 完全旁路：不改 connection.py / 对话路径 / 设备 8000 WS 协议。
- 只扩不改：低层 POST /devices/{id}/intercom 原样保留（调试/内部用）。
- 事件推送走 event_bus → /api/v1/events（红线 3）。

线程模型
--------
所有 REST 调用与 worker 均在 HTTP 服务的同一 asyncio loop 上（与
devices_routes.handle_intercom 同款 await send_to_device 模式，M1 已验证）。
状态表用 threading.Lock 保护（与项目内其他 registry 一致）。
"""

import asyncio
import threading
import time
import uuid

from fuguoai_core.utils.metrics import METRICS

TAG = __name__

CALL_TTL = 30.0        # 契约 §四/§5.1：呼叫振铃窗口 30s
POLL_INTERVAL = 1.0    # pending 补偿轮询间隔
BOOTSTRAP_SEC = 1.5          # 自举巡检周期：connected 后每隔此秒数体检一次上行
BOOTSTRAP_MAX_ROUNDS = 8     # 巡检轮数上限（覆盖 connected 后约 12s 的建链窗口）
BOOTSTRAP_MAX_RESEND = 2     # 实际重发 SetIntercom 的次数上限（有界，绝不刷屏）
BOOTSTRAP_SILENCE_SEC = 1.2  # 设备静默判定阈值：距最近上行帧超过此秒数即视为上行塌陷
BOOTSTRAP_TALK_SEC = 2.0     # 手机在发判定窗口：手机最近发帧在此秒内才认为对方在说话

# ── 上行静默看门狗（08-01 终审治标项①）──────────────────────────────
# 场景：设备应用层"假在线"——后端下行逐帧写入成功（TCP 通、send_error=0），
# 但设备上行永久归零（08-01 实测：新 WS 建立后仅发 1 帧 opus 即塌陷，
# 后端仍向其写满 2000 帧下行，手机侧全程听不到设备、设备侧 far=0）。
# 自举(bootstrap)重发 SetIntercom start 两次仍唤不回 → 判定设备侧已不可达，
# 主动收敛通话：下发 SetIntercom stop 解假在线 + 向手机推 peer_offline。
#
# 防误杀三重门（必须同时满足才动手）：
#   1. connected 已满 WATCHDOG_MIN_AGE_SEC（把自举的 2 次重发机会让完）
#   2. 设备上行静默 > WATCHDOG_SILENCE_SEC（对讲 DTX=off，设备即便静音也持续
#      发帧，故"零上行"等价于链路死，不等于"用户没说话"）
#   3. 手机侧仍在发帧（下行确实在成功写入）——排除双方都闲置的空通话被误杀
WATCHDOG_SEC = 1.0            # 看门狗巡检周期
WATCHDOG_MIN_AGE_SEC = 6.0    # connected 后的免检期（覆盖自举 2 次重发：1.5s+3.0s）
WATCHDOG_SILENCE_SEC = 5.0    # 设备上行静默判定阈值（用户拍板口径：连续 ≥5s 归零）
WATCHDOG_PHONE_ACTIVE_SEC = 5.0  # 手机在发窗口：证明下行仍在成功转发
WATCHDOG_MAX_ROUNDS = 1800    # 守望时长上限（30min），防长通话任务无界驻留

# ── 静默阈值（08-17 改为可配：stale 严格 / 曾流过放宽 / stale 兜底）────────
# 三类语义（device 指真实设备端，phone 指小程序端）：
#   - WATCHDOG_SILENCE_SEC：设备**从未流过上行**时的严格阈值（stale 孤本，
#     设备重连后从不发 uplink 帧 → 严格 5s 即拆，不留残 call）。
#   - WATCHDOG_SILENCE_SEC_AFTER_FLOW：设备**曾流过上行**后，短暂静默的放宽
#     阈值（捕捉"能恢复的短暂静默"，配合固件 SendAudio 在 MainLoop 被慢任务
#     挡导致的抖动）。默认 15s（08-20 止血：90C0 实测卡顿 8.47s>8s 触发误拆，
#     放宽到 15s 留裕量；固件 point B 落地后可视情回退）。
#   - WATCHDOG_STALE_MAX_AGE_SEC：从未流过上行 且 age 已超此值 → 直接判 stale
#     残 call 强拆（绕过"手机在发"门），消除"重连残 call age 累积 25min"孤本。
# 三者均可经 config[fuguoai][intercom] 覆盖（见 _watchdog_cfg），缺失回退默认。
WATCHDOG_SILENCE_SEC_AFTER_FLOW = 15.0
WATCHDOG_STALE_MAX_AGE_SEC = 60.0


def _watchdog_cfg(device_id: str = None):
    """读取看门狗静默阈值。

    优先级（自进化④：设备画像差异化阈值，消除"为一只设备拖累全部"）：
      1. config[fuguoai][intercom][per_device][<device_id>][...]  （单设备覆盖，最高）
      2. config[fuguoai][intercom][...]                          （全局覆盖）
      3. 模块默认常量                                          （兜底）
    per_device 缺失/非法值回退上一级，不报错。
    """
    silence = WATCHDOG_SILENCE_SEC
    silence_after = WATCHDOG_SILENCE_SEC_AFTER_FLOW
    stale_max = WATCHDOG_STALE_MAX_AGE_SEC
    # 板型 profile 作为 per-board 看门狗默认（接各种主控板子解耦层）；当前已知板值==模块默认，
    # 零回归。下面 config 全局 / per_device 覆盖仍叠加在 profile 之上（优先级更高）。
    try:
        from fuguoai_core.device_profiles import resolve_board_profile
        _bp = resolve_board_profile(device_id)
        silence = _bp.call.silence_sec
        silence_after = _bp.call.silence_after_flow_ms
        stale_max = _bp.call.stale_max_age_sec
    except Exception:
        pass
    try:
        from fuguoai_core.utils.config_holder import get_config
        icfg = ((get_config() or {}).get("fuguoai", {}) or {}).get("intercom", {}) or {}
        if isinstance(icfg.get("watchdog_silence_sec"), (int, float)):
            silence = float(icfg["watchdog_silence_sec"])
        if isinstance(icfg.get("watchdog_silence_sec_after_flow"), (int, float)):
            silence_after = float(icfg["watchdog_silence_sec_after_flow"])
        if isinstance(icfg.get("watchdog_stale_max_age_sec"), (int, float)):
            stale_max = float(icfg["watchdog_stale_max_age_sec"])
        # per-device 覆盖：如 90C0 卡顿 8.47s 需放宽到 15s，而 b4b0 维持严格
        pdev = (icfg.get("per_device") or {}) if device_id else {}
        pconf = pdev.get(device_id) if device_id else None
        if isinstance(pconf, dict):
            if isinstance(pconf.get("watchdog_silence_sec"), (int, float)):
                silence = float(pconf["watchdog_silence_sec"])
            if isinstance(pconf.get("watchdog_silence_sec_after_flow"), (int, float)):
                silence_after = float(pconf["watchdog_silence_sec_after_flow"])
            if isinstance(pconf.get("watchdog_stale_max_age_sec"), (int, float)):
                stale_max = float(pconf["watchdog_stale_max_age_sec"])
    except Exception:
        pass
    return silence, silence_after, stale_max


def _publish(event: dict):
    """旁路发布小程序实时事件（无订阅者零开销；失败绝不影响呼叫路径）。"""
    try:
        from fuguoai_core.utils.event_bus import get_event_bus
        get_event_bus().publish(event)
    except Exception:
        pass


def _set_intercom_cmd(peer: str, action: str) -> dict:
    """SetIntercom iot 指令帧（与 devices_routes.handle_intercom 完全同构）。"""
    return {
        "type": "iot",
        "commands": [{
            "name": "DeviceControl",
            "method": "SetIntercom",
            "parameters": {"peer": peer, "action": action},
        }],
    }


class IntercomCallOrchestrator:
    def __init__(self):
        self._lock = threading.Lock()
        self._calls = {}       # call_id -> record
        self._by_device = {}   # device_id -> call_id（活跃呼叫占线判定）
        self._worker_task = None

    # ────────────────────────── 呼叫建立 ──────────────────────────

    async def create_call(self, uid: str, a: str, b: str) -> dict:
        """发起呼叫（归属校验由路由层完成，a/b 均为精确存储 id）。

        返回 {"error": ...} 或呼叫单快照（status=calling|connected）。
        """
        if a == b:
            return {"error": "self_call"}
        from fuguoai_core.utils.intercom_registry import get_intercom_registry
        intercom = get_intercom_registry()
        with self._lock:
            # 编排层占线：任一端已有活跃呼叫单
            if a in self._by_device or b in self._by_device:
                return {"error": "peer_busy"}
        # 低层占线：任一端已在透传会话（含他人经低层调试接口建的）
        if intercom.peer_of(a) or intercom.peer_of(b):
            return {"error": "peer_busy"}

        now = time.time()
        rec = {
            "call_id": "call_" + uuid.uuid4().hex[:12],
            "uid": uid,
            "a": a,                  # 发起端
            "b": b,                  # 被叫端
            "status": "calling",
            "reason": None,
            "created_at": now,
            "deadline": now + CALL_TTL,
            "connected_at": None,
            "has_uplink_flowed": False,  # 设备是否曾发过上行帧（watchdog 阈值选择用）
            "ended_at": None,
        }
        with self._lock:
            self._calls[rec["call_id"]] = rec
            self._by_device[a] = rec["call_id"]
            self._by_device[b] = rec["call_id"]

        _publish(self._call_event(rec))
        # 立即尝试接通（双方都在线时秒接）；否则挂 pending 等 worker 补偿
        await self._try_connect(rec)
        self._ensure_worker()
        return self.snapshot(rec["call_id"])

    async def _try_connect(self, rec: dict):
        """双方均在线 → 低层建会话 + 双端下发 SetIntercom start。"""
        if rec["status"] != "calling":
            return
        from fuguoai_core.utils.connection_registry import get_connection_registry
        from fuguoai_core.utils.intercom_registry import get_intercom_registry
        reg = get_connection_registry()
        if not (reg.is_online(rec["a"]) and reg.is_online(rec["b"])):
            return  # 继续 pending（B-2）
        intercom = get_intercom_registry()
        result = await intercom.start(rec["a"], rec["b"])
        if result == "established":
            # B-5.2 模式B：手机端(phone-<uid>)靠 established 事件驱动录音/播放，
            # 不识别 SetIntercom iot 指令 → 跳过下发；仅向真实设备端下发 start。
            ok_a = True if rec["a"].startswith("phone-") else await reg.send_to_device(rec["a"], _set_intercom_cmd(rec["b"], "start"))
            ok_b = True if rec["b"].startswith("phone-") else await reg.send_to_device(rec["b"], _set_intercom_cmd(rec["a"], "start"))
            # 治标②（08-01 终审）：SetIntercom 未真正送达设备就别谎报 connected。
            # send_to_device 返回 False 的语义是明确的三选一——设备不在线 / session
            # 不匹配 / 发送异常，不存在"正常也返回 False"的歧义，可安全据此判失败。
            # 旧行为：仍标 connected → 手机端一路静默丢帧，直到 30s TTL 或对账才收敛。
            # 新行为：立即回滚低层会话 + failed(peer_offline)，手机秒级拿到确定结果
            # （并经 _call_event 带上 can_leave_message，直接引导去留言）。
            if not (ok_a and ok_b):
                failed_end = rec["a"] if not ok_a else rec["b"]
                alive_end = rec["b"] if not ok_a else rec["a"]
                METRICS.inc("fuguoai_intercom_send_start_fail_total", 1.0,
                            labels={"device": failed_end},
                            help_text="SetIntercom start 下发失败次数(设备离线/发送异常，呼叫直接判失败)")
                try:
                    from fuguoai_config.logger import setup_logging
                    setup_logging().bind(tag=TAG).warning(
                        f"[intercom.call] SetIntercom start 未送达 call={rec['call_id']} "
                        f"failed_end={failed_end} → 判 failed(peer_offline)，通知 {alive_end}"
                    )
                except Exception:
                    pass
                try:
                    await intercom.leave(rec["a"], "send_fail")
                except Exception:
                    pass
                await self._finish(rec, "failed", "peer_offline",
                                   offline_notify=alive_end)
                return
            with self._lock:
                rec["status"] = "connected"
                rec["connected_at"] = time.time()
            _publish(self._call_event(rec))
            try:
                from fuguoai_config.logger import setup_logging
                setup_logging().bind(tag=TAG).info(
                    f"[intercom.call] connected call={rec['call_id']} "
                    f"a={rec['a']}(sent={ok_a}) b={rec['b']}(sent={ok_b})"
                )
            except Exception:
                pass
            # 首连自举：connected 后起后台监视，覆盖「首连 OpenAudioChannel 被 WS 抖动
            # 打断、grace-resend 不覆盖」的段2 首连死（设备无上行且手机在发→重发 SetIntercom）。
            self._schedule_bootstrap(rec)
            # 上行静默看门狗（08-01 治标①）：自举唤不回时兜底收敛，别让通话
            # 挂在"下行狂写、上行全死"的假在线态上（08-01 实测持续了 41s）。
            self._schedule_watchdog(rec)
        elif result == "peer_busy":
            await self._finish(rec, "failed", "peer_busy")
        # peer_offline：is_online 刚过又掉线，留在 pending 由 worker 重试/超时

    # ────────────────────────── 首连自举（覆盖段2 首连死） ──────────────────────────

    def _schedule_bootstrap(self, rec: dict):
        """connected 后起后台守望任务。

        调度成败都落 metrics —— 07-31 排查时无法判断「自举没触发」是任务没起来还是
        判定没命中，白白多花一轮真机。scheduled_total 就是这道分水岭证据。
        """
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.get_event_loop()
            loop.create_task(self._bootstrap_monitor(rec))
            METRICS.inc("fuguoai_intercom_bootstrap_scheduled_total", 1.0,
                        help_text="自举守望任务成功创建次数(与 resend 对照可判定是任务没起还是判定没命中)")
        except Exception:
            METRICS.inc("fuguoai_intercom_bootstrap_schedule_fail_total", 1.0,
                        help_text="自举守望任务创建失败次数(无可用事件循环)")

    async def _bootstrap_monitor(self, rec: dict):
        """上行守望：connected 后持续巡检设备上行是否塌陷，塌了就重发 SetIntercom。

        判定口径（依赖 intercom_registry.relay 记的 last_frame_ts）：
        - 设备静默 = 从未发帧 或 距最近上行帧 > BOOTSTRAP_SILENCE_SEC；
        - 手机在发 = 手机最近发帧距现在 <= BOOTSTRAP_TALK_SEC。
        两者同时满足 → 重发 SetIntercom start，促使固件重开音频通道。

        🔴 关键教训（07-31 实测）：设备常在 connected 后发 ~7 帧（约 140ms）便塌陷。
        旧实现在首轮体检发现「设备刚发过帧」就 return 永久放弃，导致自举从未触发
        （metrics bootstrap_resend_total 恒为空）。现改为**持续巡检 continue**，
        只有会话结束/非 phone↔device/重发次数用尽才退出，且重发次数硬上限有界。

        无论重发成功与否，都会记 uplink_stall_total —— 即便固件不响应，运维也能从
        /metrics 一眼看出「设备上行塌陷」发生过几次，不再依赖被吞的 stdlib 日志。
        """
        from fuguoai_core.utils.intercom_registry import get_intercom_registry
        from fuguoai_core.utils.connection_registry import get_connection_registry
        resend_left = BOOTSTRAP_MAX_RESEND
        for _ in range(BOOTSTRAP_MAX_ROUNDS):
            await asyncio.sleep(BOOTSTRAP_SEC)
            if resend_left <= 0:
                return
            with self._lock:
                if rec["status"] != "connected":
                    return
                a, b = rec["a"], rec["b"]
            # 识别 phone↔device 两端（phone 端 id 形如 phone-<uid>）
            if a.startswith("phone-"):
                phone, device = a, b
            elif b.startswith("phone-"):
                phone, device = b, a
            else:
                return  # 设备↔设备，无需自举
            intercom = get_intercom_registry()
            dev_age = intercom.last_frame_age(device)
            phone_age = intercom.last_frame_age(phone)
            device_silent = (dev_age is None) or (dev_age > BOOTSTRAP_SILENCE_SEC)
            phone_talking = (phone_age is not None) and (phone_age <= BOOTSTRAP_TALK_SEC)
            if not (device_silent and phone_talking):
                continue  # 设备上行健在 或 手机没在说话 → 本轮跳过，继续守望
            # 上行塌陷事实先落 metrics（与重发是否成功解耦，便于定责）
            METRICS.inc("fuguoai_intercom_uplink_stall_total", 1.0,
                        labels={"device": device},
                        help_text="检测到设备上行塌陷的次数(手机在说话但设备停止上行)")
            peer_of_device = intercom.peer_of(device)
            if not peer_of_device:
                continue
            try:
                reg = get_connection_registry()
                await reg.send_to_device(device, _set_intercom_cmd(peer_of_device, "start"))
                resend_left -= 1
                METRICS.inc("fuguoai_intercom_bootstrap_resend_total", 1.0,
                            labels={"device": device},
                            help_text="自举重发 SetIntercom start 次数(唤回塌陷的设备音频通道)")
                try:
                    from fuguoai_config.logger import setup_logging
                    setup_logging().bind(tag=TAG).warning(
                        f"[intercom.call] bootstrap 重发 SetIntercom start 至设备 {device} "
                        f"peer={peer_of_device} dev_age={dev_age} phone_age={phone_age}"
                    )
                except Exception:
                    pass
            except Exception:
                METRICS.inc("fuguoai_intercom_bootstrap_resend_fail_total", 1.0,
                            labels={"device": device},
                            help_text="自举重发 SetIntercom 失败次数(设备连接已不可用)")

    # ────────────────────── 上行静默看门狗（治标①） ──────────────────────

    def _schedule_watchdog(self, rec: dict):
        """connected 后起看门狗任务（调度成败落 metrics，口径同 bootstrap）。"""
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.get_event_loop()
            loop.create_task(self._uplink_watchdog(rec))
            METRICS.inc("fuguoai_intercom_watchdog_scheduled_total", 1.0,
                        help_text="上行静默看门狗任务成功创建次数")
        except Exception:
            METRICS.inc("fuguoai_intercom_watchdog_schedule_fail_total", 1.0,
                        help_text="上行静默看门狗任务创建失败次数(无可用事件循环)")

    async def _uplink_watchdog(self, rec: dict):
        """守望「下行成功但设备上行连续归零」的假在线态，命中即主动收敛通话。

        为什么"零上行"可以直接判死而不会误伤沉默用户：对讲链路 DTX=off
        （固件 fix10 明确关闭，DTX=on 曾导致远端静音），设备只要音频通道活着
        就持续发帧，静音也发。所以上行完全归零 = 应用层链路死，不是没说话。

        为什么还要看"手机在发"：last_frame_ts 只在 relay **成功转发后**才更新，
        故手机侧新鲜 = 后端确实在把下行写进设备连接。若手机也不发，说明是空闲
        通话（或手机侧问题），不属本看门狗职责，不动手。

        命中动作（与用户 08-01 拍板口径一致）：
          1. 下发 SetIntercom stop 解设备假在线（经 _finish(send_stop=True)）
          2. 向手机推 peer_offline，让小程序立刻收尾而不是干等
          3. 呼叫单收敛为 ended(peer_unreachable)，释放占线，/call/active 不留僵尸
        """
        from fuguoai_core.utils.intercom_registry import get_intercom_registry
        from fuguoai_core.utils.intercom_registry import _send_event as intercom_send_event
        for _ in range(WATCHDOG_MAX_ROUNDS):
            await asyncio.sleep(WATCHDOG_SEC)
            with self._lock:
                if rec["status"] != "connected":
                    return  # 已挂断/已收敛，守望结束
                a, b = rec["a"], rec["b"]
                connected_at = rec["connected_at"] or 0
            # 仅 phone↔device 会话适用（设备↔设备无手机侧参照，且不在本次故障域）
            if a.startswith("phone-"):
                phone, device = a, b
            elif b.startswith("phone-"):
                phone, device = b, a
            else:
                return
            # 门 1：免检期——先把自举的两次重发机会让完，避免和 bootstrap 抢
            if time.time() - connected_at < WATCHDOG_MIN_AGE_SEC:
                continue
            intercom = get_intercom_registry()
            dev_age = intercom.last_frame_age(device)
            phone_age = intercom.last_frame_age(phone)
            silence, silence_after, stale_max = _watchdog_cfg(device)
            # 记录"设备是否曾发过上行帧"——决定用严格阈值还是放宽阈值
            with self._lock:
                if dev_age is not None and dev_age < silence:
                    rec["has_uplink_flowed"] = True
                has_flowed = rec.get("has_uplink_flowed", False)
            # 门 2：设备上行静默阈值
            #   - 曾流过 → 放宽（捕捉"能恢复的短暂静默"，配合固件 SendAudio 抖动）
            #   - 从未流过 → 严格（stale 孤本：设备重连从不发 uplink）
            thr = silence_after if has_flowed else silence
            device_silent = (dev_age is None) or (dev_age > thr)
            # 门 3：手机仍在发（证明下行正在成功写入设备连接）
            phone_active = (phone_age is not None) and (phone_age <= WATCHDOG_PHONE_ACTIVE_SEC)
            # stale 兜底：设备上行静默(或从未发帧)已超 stale_max → 直接判死拆，
            # 绕过门3（手机可能也不发），消除"重连残 call age 累积 25min"孤本。
            # 关键修正：has_uplink_flowed 一旦置 True 便永不清零（记录"本 call 是否曾健康"），
            # 故不能用 not has_flowed 作门槛——否则"曾流过→重连后静默"的真 stale 残call
            # 永远进不了 stale_dead，25min 孤本治不掉。改用"有效静默时长"判定：
            #   - 发过帧(dev_age 非 None) → 以 dev_age 计；
            #   - 从未发帧(dev_age 为 None，_last_frame_ts 无记录) → 以 call 年龄计。
            if dev_age is not None:
                _eff_silent = dev_age
            else:
                _eff_silent = time.time() - connected_at
            stale_dead = _eff_silent > stale_max
            if not (device_silent and (phone_active or stale_dead)):
                continue
            METRICS.inc("fuguoai_intercom_watchdog_teardown_total", 1.0,
                        labels={"device": device},
                        help_text="看门狗判定设备假在线并主动收敛通话的次数(下行成功但上行连续归零)")
            try:
                from fuguoai_config.logger import setup_logging
                setup_logging().bind(tag=TAG).warning(
                    f"[intercom.call] watchdog 判定设备{'stale残call' if stale_dead else '假在线'} "
                    f"device={device} dev_age={dev_age} phone_age={phone_age} "
                    f"has_flowed={has_flowed} stale_dead={stale_dead} call={rec['call_id']} "
                    f"→ 下发 SetIntercom stop + 推 peer_offline 并收敛通话"
                )
            except Exception:
                pass
            # 先通知手机（收敛过程中低层会话会被拆，事件要赶在拆除前送达）
            try:
                await intercom_send_event(phone, "peer_offline", device)
            except Exception:
                pass
            await self._finish(rec, "ended", "peer_unreachable", send_stop=True)
            return

    async def notify_online(self, device_id: str):
        """B-5.3 · 某端 WS 刚上线（phone hello / 设备重连）→ 立即补偿接通其
        pending 呼叫，消除 POST /intercom/call 与 hello 到达顺序竞态带来的
        最多 1s worker 延迟。无涉及该端的 calling 单则静默返回。"""
        with self._lock:
            cid = self._by_device.get(device_id)
            rec = self._calls.get(cid) if cid else None
        if rec and rec["status"] == "calling":
            try:
                await self._try_connect(rec)
            except Exception:
                pass

    # ────────────────────────── 挂断 / 收敛 ──────────────────────────

    async def end_call(self, uid: str, call_id: str) -> dict:
        with self._lock:
            rec = self._calls.get(call_id)
            if not rec or rec["uid"] != uid:
                return {"error": "not_found"}
            if rec["status"] in ("ended", "failed"):
                return self._snap(rec)
        await self._finish(rec, "ended", "hangup", send_stop=True)
        return self.snapshot(call_id)

    async def _finish(self, rec: dict, status: str, reason: str, send_stop: bool = False,
                      offline_notify: str = None):
        """终态收敛：拆低层会话、通知设备与小程序、释放占线。

        offline_notify（08-01 新增，可选）：failed(peer_offline) 时把 peer_offline
        事件推给指定的"存活端"。默认 None 保持旧行为（推给发起端 rec["a"]）——
        旧行为在"发起端自己就是掉线那一端"时会把事件推给死连接，等于没通知。
        """
        with self._lock:
            if rec["status"] in ("ended", "failed"):
                return
            rec["status"] = status
            rec["reason"] = reason
            rec["ended_at"] = time.time()
            self._by_device.pop(rec["a"], None)
            self._by_device.pop(rec["b"], None)
        from fuguoai_core.utils.connection_registry import get_connection_registry
        from fuguoai_core.utils.intercom_registry import get_intercom_registry
        from fuguoai_core.utils.intercom_registry import _send_event as intercom_send_event
        intercom = get_intercom_registry()
        reg = get_connection_registry()
        try:
            if send_stop:
                # 主动挂断：拆会话（对端自然收 peer_left）+ 向真实设备端下发 stop
                # （手机端 phone-<uid> 靠 peer_left 事件收尾，不下发 SetIntercom stop）
                await intercom.leave(rec["a"], "stop")
                if not rec["a"].startswith("phone-"):
                    await reg.send_to_device(rec["a"], _set_intercom_cmd(rec["b"], "stop"))
                if not rec["b"].startswith("phone-"):
                    await reg.send_to_device(rec["b"], _set_intercom_cmd(rec["a"], "stop"))
            elif status == "failed" and reason == "peer_offline":
                # 超时未接通：给存活端推 peer_offline（其收尾 StopIntercom）
                notify_to = offline_notify or rec["a"]
                peer_of_notify = rec["b"] if notify_to == rec["a"] else rec["a"]
                await intercom_send_event(notify_to, "peer_offline", peer_of_notify)
        except Exception:
            pass
        _publish(self._call_event(rec))

    # ────────────────────────── B-2 worker ──────────────────────────

    def _ensure_worker(self):
        if self._worker_task is None or self._worker_task.done():
            try:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = asyncio.get_event_loop()
                self._worker_task = loop.create_task(self._worker())
            except Exception:
                self._worker_task = None

    async def _worker(self):
        """每 1s：calling 单补偿重试/超时判定；connected 单与低层会话对账。"""
        from fuguoai_core.utils.intercom_registry import get_intercom_registry
        intercom = get_intercom_registry()
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            with self._lock:
                active = [r for r in self._calls.values()
                          if r["status"] in ("calling", "connected")]
            for rec in active:
                try:
                    if rec["status"] == "calling":
                        if time.time() > rec["deadline"]:
                            await self._finish(rec, "failed", "peer_offline")
                        else:
                            await self._try_connect(rec)
                    elif rec["status"] == "connected":
                        # 低层会话已消亡（设备挂断/12s 兜底）→ 呼叫单收敛 ended
                        if not intercom.peer_of(rec["a"]) and not intercom.peer_of(rec["b"]):
                            await self._finish(rec, "ended", "peer_left")
                except Exception:
                    pass
            # 终态呼叫单保留 10 分钟供 /active 查询后清理
            cutoff = time.time() - 600
            with self._lock:
                for cid in [c for c, r in self._calls.items()
                            if r["status"] in ("ended", "failed")
                            and (r["ended_at"] or 0) < cutoff]:
                    self._calls.pop(cid, None)

    # ────────────────────────── 查询 / 快照 ──────────────────────────

    def _snap(self, rec: dict) -> dict:
        return {
            "call_id": rec["call_id"],
            "status": rec["status"],
            "reason": rec["reason"],
            "my_device_id": rec["a"],
            "target_device_id": rec["b"],
            "created_at": int(rec["created_at"]),
            "connected_at": int(rec["connected_at"]) if rec["connected_at"] else None,
        }

    def snapshot(self, call_id: str):
        with self._lock:
            rec = self._calls.get(call_id)
            return self._snap(rec) if rec else None

    def active_for_user(self, uid: str):
        """该用户最近一张非终态呼叫单（无则 None）。"""
        with self._lock:
            cands = [r for r in self._calls.values()
                     if r["uid"] == uid and r["status"] in ("calling", "connected")]
            if not cands:
                return None
            return self._snap(max(cands, key=lambda r: r["created_at"]))

    def _call_event(self, rec: dict) -> dict:
        """小程序实时事件（B-4，device_id 用发起端保证归属过滤命中）。"""
        ev = {
            "type": "intercom.call",
            "call_id": rec["call_id"],
            "status": rec["status"],
            "device_id": rec["a"],
            "peer_device_id": rec["b"],
            "ts": int(time.time()),
        }
        if rec["reason"]:
            ev["reason"] = rec["reason"]
        # 留言功能挂接（VOICEMAIL_DESIGN §7）：呼叫失败(对端离线)时提示可留言。
        # 只增字段，老客户端无感；不改状态机、不占 _by_device。
        if rec["status"] == "failed" and rec["reason"] == "peer_offline":
            ev["can_leave_message"] = True
        return ev


_ORCH = None


def get_call_orchestrator() -> IntercomCallOrchestrator:
    global _ORCH
    if _ORCH is None:
        _ORCH = IntercomCallOrchestrator()
    return _ORCH
