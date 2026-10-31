"""对讲（Intercom）1:1 透传中继注册表（契约 v1.6.0 Part B）。

旁路非侵入：不入对话路径、不进 VAD/ASR/TTS/LLM。
connection.py 的 _route_message 在 intercom_mode 下把二进制帧交给 relay() 转发对端。
设备↔设备(M2/loopback)零转码原样透传（裸 opus 净荷，自 2026-08-01 起无 4B seq 头）；
role=phone 会话由 relay() 在边界做 opus<->PCM(可选 AAC) 双向转码（方案 B，见 _transcode）。
手机侧编码由 hello 的 audio_codec 协商：默认 pcm（640B 裸 PCM，兼容旧小程序），
声明 "aac" 时走微信原生 AAC/ADTS（见 AacTranscoder）。

会话模型（MVP 设备↔设备 1:1）：
- _sessions: sid -> {"a": device_id, "b": device_id, "state", "created_at", "grace_deadline"}
- _by_device: device_id -> sid   （忙线判定 O(1)；loopback 会话不落此表）
- _loopback: set(device_id)      （loopback 自环调试模式，独立集合，
                                   不污染忙线判定——联调反馈 2026-07-24）

事件（走各端自己的 WS 文本帧，契约 B.9）：
  {"type":"intercom","event":"established"|"peer_offline"|"peer_left"|"peer_busy",
   "peer":"<device_id>",
   "session_id":"<sid>"}   # established 事件附带后端会话 sid（契约要求；固件握手可回带以对齐）
注：relay 会话绑定以 device_id 为准（_by_device 映射），session_id 仅作契约合规与对端对账用，
不影响转发路由（转发路由见 relay()）。

断连兜底：on_disconnect 不立即拆会话，标 grace + GRACE_SEC 定时器；
期间对端帧丢弃；超时对端收 peer_left 后拆除；grace 内同 device_id 重连恢复。

线程安全：所有读写走一把 threading.Lock（转发热路径仅查表，无 IO 持锁）。
发送：经 connection_registry 的 send factory 拿 conn →
  文本 conn.safe_send / 二进制 conn.safe_send_bytes（同一把 _send_lock 串行）。
"""

import asyncio
import collections
import json
import logging
import os
import struct
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import numpy as np

import opuslib_next

from fuguoai_core.utils.aac_transcoder import AacTranscoder
from fuguoai_core.utils.intercom_stats import get_stats_hub
from fuguoai_core.utils.metrics import METRICS

TAG = __name__
_logger = logging.getLogger(TAG)

# ── 临时诊断（联调用，稳定后删除本段）──
# 生产 root logger 级别=WARNING，本模块的 INFO 诊断(relay到达/设备→手机/手机→设备/leave/
# on_disconnect/on_reconnect)会被吞；挂独立 INFO handler 确保诊断在 docker logs 可见，
# 且仅影响本模块、不污染全局日志级别。
import sys as _sys
_diag_h = logging.StreamHandler(_sys.stderr)
_diag_h.setLevel(logging.INFO)
_logger.addHandler(_diag_h)
_logger.setLevel(logging.INFO)
_logger.propagate = False

# ── W3: 对讲 relay CPU 下沉（事件循环强化，2026-09-25）──
# relay() 热路径上 AEC 谱减 + opus<->PCM 转码 + 自喂 ref 解码/喂参考 是纯 CPU 工作，
# 原在事件循环线程同步执行（connection.py:673 逐帧 await relay；_flush_grace:458 重放亦如此），
# 单通对讲高码率下占用主 loop 时间片，挤占 WS 收发/其他会话调度。
# 下沉到专属【单 worker】有界线程池：① 单 worker 保证每设备逐帧 FIFO（转发语义不变）；
# ② AEC 与 ref 缓存读写在同一 worker 内串行，天然规避迭代 vs 修改竞态；
# ③ safe_send_bytes 仍在主 loop（WS 写必须 loop 线程，且同一把 _send_lock 串行，跨设备不串帧）。
# 默认关闭（FUGUOAI_INTERCOM_RELAY_OFFLOAD 未置 1/true）：避免线上立即生效，真机 A/B 由运维
# 置环境变量后灰度。守先别固：不 docker commit，待听感确认。
_INTERCOM_RELAY_EXECUTOR = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="intercom_relay"
)


def _intercom_relay_offload_enabled() -> bool:
    v = os.environ.get("FUGUOAI_INTERCOM_RELAY_OFFLOAD", "")
    return v.strip().lower() in ("1", "true", "yes", "on")


# ── 日志限频（2026-08-01）──
# relay 早退 / 转码失败原本每帧一条 WARNING：单通对讲（设备离线态）刷出 3190 行，
# 一次联调 10555 行占当日日志 80%，既淹没真实告警又吃日志配额。
# 改为同 key 每 _THROTTLE_SEC 秒最多一条，并在行尾折叠期间条数——诊断价值不变，
# 精确计数一律以 /metrics 的 fuguoai_intercom_relay_drop_total 为准。
_THROTTLE_SEC = 2.0
_throttle_state: dict = {}


def _throttled_warn(key: str, msg: str, interval: float = _THROTTLE_SEC):
    """限频 WARNING：同 key 每 interval 秒最多一条，附带被折叠的同类条数。

    仅在 relay 热路径（单事件循环线程）调用，GIL 下 dict 读改足够安全，
    故不取锁——避免给逐帧路径加锁开销。
    """
    now = time.time()
    st = _throttle_state.get(key)
    if st is None:
        st = {"t": 0.0, "n": 0}
        _throttle_state[key] = st
    st["n"] += 1
    if now - st["t"] < interval:
        return
    n = st["n"]
    st["n"] = 0
    st["t"] = now
    _logger.warning(msg + (f"（近 {interval:.0f}s 同类 {n} 条已折叠）" if n > 1 else ""))

# 断连兜底窗口（07-26 联调：设备 WS 抖动频繁，12s 过严苛→活跃通话瞬时断连即被拆会话；
# 放宽到 30s 给设备重连恢复留足余地，属临时诊断期放宽，稳定后评估是否回调）
GRACE_SEC = 30.0
# grace 期缓冲帧上限(帧数)。20ms/帧 → 150 帧≈3s。突发重连风暴下避免内存暴涨,
# 超出丢弃最旧帧(对应已无意义的过去音频)。见 relay/_buffer_grace_frame/_flush_grace。
GRACE_BUFFER_MAX = 150


def _get_conn(device_id: str):
    """经 connection_registry 的 send factory 拿活跃 connection（不在线返回 None）。

    一致性判据统一走 connection_registry._same_conn：两端登记项都带 conn_uid 时按
    连接实例精确比对，否则退化 session_id。固件对主对话 WS 与 intercom 音频 WS 复用
    同一 session_id，仅比对 session_id 会把不同连接误判成同一条（2026-08-02）。
    """
    try:
        from fuguoai_core.utils.connection_registry import (
            get_connection_registry, _same_conn,
        )
        reg = get_connection_registry()
        with reg._lock:
            send_rec = reg._send.get(device_id)
            online_rec = reg._online.get(device_id)
            if not _same_conn(online_rec, send_rec):
                return None
            factory = send_rec["factory"]
        return factory()
    except Exception:
        return None


async def _send_event(device_id: str, event: str, peer: str = None, session_id: str = None):
    """向某端下发 intercom 文本事件（失败静默，绝不抛给调用方）。

    B-4（契约 v1.8r1 C6）：同步旁路发布到 event_bus，供小程序经
    /api/v1/events 实时收 intercom 事件（无订阅者时 publish 为 no-op，
    零开销；发布失败绝不影响设备侧下发）。
    """
    try:
        from fuguoai_core.utils.event_bus import get_event_bus
        ev = {"type": "intercom", "event": event, "device_id": device_id,
              "ts": int(time.time())}
        if peer is not None:
            ev["peer"] = peer
        if session_id is not None:
            ev["session_id"] = session_id
        get_event_bus().publish(ev)
    except Exception:
        pass
    try:
        conn = _get_conn(device_id)
        if conn is None:
            return False
        payload = {"type": "intercom", "event": event}
        if peer is not None:
            payload["peer"] = peer
        # 契约对齐：设备侧 established 也必须带 session_id（联调契约.md §B.6.4/§B.9
        # 要求按 session_id 绑定 1:1 音频会话）。否则固件侧"等 session_id 才开通道"
        # 的守卫（application.cc:181-184，被 2408 放宽）会永远等不到 → 通道不开。
        # 路由仍以 device_id 为准（relay 不读此字段），仅作契约/对账用，向后兼容。
        if session_id is not None:
            payload["session_id"] = session_id
        await conn.safe_send(json.dumps(payload, ensure_ascii=False))
        return True
    except Exception:
        return False


class IntercomRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions = {}     # sid -> record
        self._by_device = {}    # device_id -> sid（不含 loopback）
        self._loopback = set()  # loopback 模式的 device_id 集合
        self._grace_tasks = {}  # device_id -> asyncio.Task
        self._phone_codecs = {}  # phone_id -> {"enc","dec","aac","seq"}  (方案B opus<->PCM<->AAC)
        self._codec_lock = threading.Lock()
        # 留言虚拟对端播放中的设备集合：其 mic 上行帧在 relay 首行静默丢弃
        # （不注册会话 → 不占忙线表；无本集合则 relay 每帧刷"未注册"warning 刷屏）
        self._vm_sinks = set()
        # 各端最近一次经 relay 成功转发(发出)的墙钟时刻：供首连自举判定
        # 「设备上行是否存活」(段2 首连死诊断用)。键=device_id。
        self._last_frame_ts = {}
        # grace 期缓冲：对端短暂断连(突发重连风暴)期间, 仍在线一端发来的帧先入此缓冲,
        # 对端 grace 内重连恢复(established)时由 _flush_grace 补齐, 避免对讲音频成片丢失。
        # 键=sid, 值=collections.deque(maxlen=GRACE_BUFFER_MAX), 元素=(from_device_id, frame)。
        self._grace_buffers = {}
        # B2 服务端 AEC 实例：device_id -> IntercomAec。仅在该设备 hello 协商开启
        # 服务端 AEC 后存在；会话拆除/断连时回收（opus 状态不可泄漏）。
        self._aec_instances = {}
        # B2 outband 自喂参考(ref)：后端把下行(→设备)的 peer 音频解码为 16k mono PCM，
        # 按服务端发送时刻存入，供该设备的服务端 AEC 作回声参考（不依赖固件 audio_playback_ref）。
        # 与 chat 自喂参考同源思路；device_id -> {ts_ms: pcm_bytes}。
        self._intercom_ref = {}
        self._intercom_ref_time = {}   # device_id -> {ts_ms: time()}
        self._aec_modes = {}           # device_id -> "2ch" | "outband" | "2ch_srvref"
        self._aec_max_delay = {}       # device_id -> int(ms)，时延搜索窗口上限
        self._aec_echo_delay = {}      # device_id -> int(ms)，回声延迟带中心(长延迟设备)
        self._aec_echo_band = {}       # device_id -> int(ms)，回声延迟带半宽
        self._ref_decoders = {}        # device_id -> opuslib Decoder(16000,1)（自喂 ref 解码用）

    # ── 留言播放 sink（voicemail_player 专用） ──────────────────────────

    def vm_sink_add(self, device_id: str):
        with self._lock:
            self._vm_sinks.add(device_id)

    def vm_sink_remove(self, device_id: str):
        with self._lock:
            self._vm_sinks.discard(device_id)

    # ────────────────────────── 会话建立 / 拆除 ──────────────────────────

    async def start(self, caller_id: str, peer_id: str) -> str:
        """建立会话。返回 "established" | "peer_offline" | "peer_busy"。

        loopback：peer_id == "loopback" 优先匹配 → 直接自环，不查在线状态、
        不落 _by_device（避免污染忙线判定），固件完全无感知。
        """
        if not caller_id or not peer_id:
            return "peer_offline"

        # loopback 自环（M1 调试模式，一台板子即可验 AEC 消回授）
        if peer_id == "loopback":
            with self._lock:
                self._loopback.add(caller_id)
            await _send_event(caller_id, "established", "loopback")
            return "established"

        from fuguoai_core.utils.connection_registry import get_connection_registry
        online = get_connection_registry()

        with self._lock:
            # 忙线拒绝：任一端已有会话（不抢占，契约 B.6）
            if caller_id in self._by_device or peer_id in self._by_device:
                busy = True
            else:
                busy = False
        if busy:
            await _send_event(caller_id, "peer_busy", peer_id)
            return "peer_busy"

        if not online.is_online(peer_id):
            await _send_event(caller_id, "peer_offline", peer_id)
            return "peer_offline"

        sid = uuid.uuid4().hex
        with self._lock:
            self._sessions[sid] = {
                "a": caller_id,
                "b": peer_id,
                "state": "established",
                "created_at": time.time(),
                "grace_deadline": 0.0,
                "grace_device": None,
            }
            self._by_device[caller_id] = sid
            self._by_device[peer_id] = sid
        get_stats_hub().session_start(sid, caller_id, peer_id)
        await _send_event(caller_id, "established", peer_id, session_id=sid)
        await _send_event(peer_id, "established", caller_id, session_id=sid)
        return "established"

    async def leave(self, device_id: str, reason: str = "stop"):
        """主动挂断：对端收 peer_left，拆除会话。loopback 直接摘除。"""
        peer = None
        with self._lock:
            if device_id in self._loopback:
                self._loopback.discard(device_id)
                self.disable_intercom_aec(device_id)  # 回收可能已开启的服务端 AEC 实例
                return
            sid = self._by_device.pop(device_id, None)
            if sid:
                rec = self._sessions.pop(sid, None)
                self._grace_buffers.pop(sid, None)  # 主动挂断: 缓冲帧丢弃
                if rec:
                    peer = rec["b"] if rec["a"] == device_id else rec["a"]
                    self._by_device.pop(peer, None)
        _logger.info(f"[intercom][debug] leave: {device_id} reason={reason} peer={peer}")
        if peer:
            get_stats_hub().session_end(sid, reason)
        self._cancel_grace(device_id)
        self._drop_phone_codec(device_id)
        self.disable_intercom_aec(device_id)  # 回收服务端 AEC 实例（opus 状态不可泄漏）
        if peer:
            self._cancel_grace(peer)
            self._drop_phone_codec(peer)
            self.disable_intercom_aec(peer)
            await _send_event(peer, "peer_left", device_id)

    # ────────────────────────── 帧转发（热路径） ──────────────────────────

    async def relay(self, from_device_id: str, frame: bytes) -> bool:
        """透传二进制帧；对 role=phone 会话自动做 opus<->PCM 双向转码（方案 B）。

        loopback：回发给自己。grace 期：丢弃。对端不在线：丢弃（grace 定时器管拆除）。
        仅当会话一端为 phone、一端为设备时转码；设备↔设备(M2/loopback)零转码原样透传。

        可观测性：每帧的转发/丢弃都记 METRICS 计数器（fuguoai_intercom_relay_*），
        经 /metrics 抓取，脱离 stdlib logging 盲区（docker 不桥接本模块 stderr 诊断）。
        """
        # 留言播放中：设备 mic 上行帧静默丢弃（单向收流，无对端）
        with self._lock:
            if from_device_id in self._vm_sinks:
                METRICS.inc("fuguoai_intercom_relay_drop_total", 1.0,
                            labels={"reason": "vm_sink"},
                            help_text="Intercom relay 丢弃帧计数（按原因）")
                return False
        # ── 临时诊断：到达帧计数 + 注册态/会话态/帧头（确认两端是否真发了帧、格式对不对）──
        self._relay_debug_bytes(from_device_id, len(frame), frame)
        with self._lock:
            if from_device_id in self._loopback:
                target = from_device_id
            else:
                sid = self._by_device.get(from_device_id)
                if not sid:
                    METRICS.inc("fuguoai_intercom_relay_drop_total", 1.0,
                                labels={"reason": "not_registered"},
                                help_text="Intercom relay 丢弃帧计数（按原因）")
                    _throttled_warn(
                        f"early_notreg:{from_device_id}",
                        f"[intercom][debug] relay 早退: {from_device_id} 未在 _by_device 注册(未进透传?)",
                    )
                    return False
                rec = self._sessions.get(sid)
                if not rec:
                    METRICS.inc("fuguoai_intercom_relay_drop_total", 1.0,
                                labels={"reason": "session_not_established"},
                                help_text="Intercom relay 丢弃帧计数（按原因）")
                    _throttled_warn(
                        f"early_state:{from_device_id}",
                        f"[intercom][debug] relay 早退: {from_device_id} session={sid} "
                        f"state=None(会话不存在)",
                    )
                    return False
                st = rec["state"]
                if st == "grace":
                    # 对端短暂断连(突发重连风暴)：缓冲帧, 重连恢复(established)时补齐, 不丢音频。
                    self._buffer_grace_frame(sid, from_device_id, frame)
                    return False
                if st != "established":
                    # ringing 等未建立态：丢弃(呼叫尚未接通, 无对端可投递)
                    METRICS.inc("fuguoai_intercom_relay_drop_total", 1.0,
                                labels={"reason": "session_not_established"},
                                help_text="Intercom relay 丢弃帧计数（按原因）")
                    _throttled_warn(
                        f"early_state:{from_device_id}",
                        f"[intercom][debug] relay 早退: {from_device_id} session={sid} "
                        f"state={st}(非established)",
                    )
                    return False
                # st == "established"：下行继续设 target 并转码转发
                target = rec["b"] if rec["a"] == from_device_id else rec["a"]
        conn = _get_conn(target)
        if conn is None:
            METRICS.inc("fuguoai_intercom_relay_drop_total", 1.0,
                        labels={"reason": "peer_offline"},
                        help_text="Intercom relay 丢弃帧计数（按原因）")
            _throttled_warn(
                f"early_peeroff:{target}",
                f"[intercom][debug] relay 早退: 对端 {target} 无活跃连接(conn=None，对方已断开)",
            )
            return False
        # phone 编码模式：由手机 hello 的 audio_codec 协商（默认 pcm）。
        # 下行时 target 即手机(conn 已是它)；上行时 source 为手机，另取其 conn。
        phone_mode = "pcm"
        if target.startswith("phone-"):
            phone_mode = getattr(conn, "phone_audio_codec", "pcm") or "pcm"
        elif from_device_id.startswith("phone-"):
            pconn = _get_conn(from_device_id)
            if pconn is not None:
                phone_mode = getattr(pconn, "phone_audio_codec", "pcm") or "pcm"
        # ── W3 CPU 段下沉：AEC 谱减 + 转码 + 自喂 ref 解码/喂参考 ──
        # 原在事件循环线程同步执行；_relay_compute_async 据 env 开关（FUGUOAI_INTERCOM_RELAY_OFFLOAD）
        # 决定是否下沉到专属单 worker 线程池（见模块顶部 _INTERCOM_RELAY_EXECUTOR）。
        # 返回 None 等同 transcode_fail 丢帧；锁外 CPU 工作不阻塞其他线程取锁。
        out = await self._relay_compute_async(frame, from_device_id, target, phone_mode)
        if out is None:
            METRICS.inc("fuguoai_intercom_relay_drop_total", 1.0,
                        labels={"reason": "transcode_fail"},
                        help_text="Intercom relay 丢弃帧计数（按原因）")
            return False
        try:
            await conn.safe_send_bytes(out)
            # 成功转发：按对端类型记 downlink(→设备) / uplink(→手机) 计数；
            # 同时记来源端最近发帧时刻，供首连自举判定设备上行是否存活。
            with self._lock:
                self._last_frame_ts[from_device_id] = time.time()
            if target.startswith("phone-"):
                METRICS.inc("fuguoai_intercom_relay_uplink_total", 1.0,
                            labels={"from": from_device_id, "to": target},
                            help_text="Intercom relay 成功转发到手机的帧(设备→手机)；为 0 表示设备没上行")
            else:
                METRICS.inc("fuguoai_intercom_relay_downlink_total", 1.0,
                            labels={"from": from_device_id, "to": target},
                            help_text="Intercom relay 成功转发到设备的帧(手机→设备 / 设备→设备)；>0 表示后端已写入设备 WS")
            return True
        except Exception:
            METRICS.inc("fuguoai_intercom_relay_drop_total", 1.0,
                        labels={"reason": "send_error"},
                        help_text="Intercom relay 丢弃帧计数（按原因）")
            return False

    async def _relay_compute_async(self, frame, from_device_id, target, phone_mode):
        """W3 调度层：据 env 开关决定是否把对讲 relay 的纯 CPU 段下沉到专属单 worker 线程池。

        默认不开启（FUGUOAI_INTERCOM_RELAY_OFFLOAD 未置 1/true）→ 走原逻辑（直接在当前
        事件循环线程同步计算），行为与改造前完全一致，零线上风险。
        开启后 → 经 _INTERCOM_RELAY_EXECUTOR(单 worker) 离主 loop 执行；单 worker 保证每设备
        逐帧 FIFO，且 AEC/ref 读写同线程串行，规避竞态。safe_send_bytes 仍在主 loop（见 relay）。
        """
        if _intercom_relay_offload_enabled():
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                _INTERCOM_RELAY_EXECUTOR,
                self._relay_compute, frame, from_device_id, target, phone_mode)
        return self._relay_compute(frame, from_device_id, target, phone_mode)

    def _relay_compute(self, frame, from_device_id, target, phone_mode):
        """W3 计算层（同步，可跑在事件循环线程或下沉线程）：AEC 谱减 + 转码 + 自喂 ref 解码/喂参考。

        与改造前 relay() CPU 段语义完全一致。任何异常一律降级为 None（=transcode_fail 丢帧），
        绝不向事件循环抛异常。teardown(disable_intercom_aec) 仅 pop 外层 dict 键、不迭代内层 dict，
        故与断连回收无竞态（最坏=对端已断开时重建一条无用 ref，无崩溃）。
        """
        try:
            # ── B2 全双工服务端 AEC（双端）──
            if from_device_id.startswith("phone-"):
                # 仅 pcm 模式上行做 AEC（aac 模式帧为 AAC 编码字节，非裸 PCM，
                # 当前部署默认 pcm；aac 手机暂跳过 phone AEC 降级透传，避免把 AAC 当 PCM 损坏）。
                if phone_mode == "pcm":
                    frame = self._intercom_aec_process_phone(from_device_id, frame)
            else:
                frame = self._intercom_aec_process(from_device_id, frame)
            out = self._transcode(frame, from_device_id, target, phone_mode)
            if out is None:
                return None
            # ── B2 outband 自喂参考(ref) ──
            _tgt_aec_mode = self._aec_modes.get(target)
            if _tgt_aec_mode in ("outband", "2ch_srvref", "2ch"):
                try:
                    if target.startswith("phone-"):
                        _pmode = getattr(_get_conn(target), "phone_audio_codec", "pcm") or "pcm"
                        if _pmode == "aac":
                            _pcodec = self._get_phone_codec(target, "aac")
                            _aac = _pcodec.get("aac")
                            pcm = _aac.decode(bytes(out)) if _aac is not None else bytes(out)
                        else:
                            pcm = bytes(out)
                    else:
                        dec = self._get_ref_decoder(target)
                        pcm = dec.decode(bytes(out), 2880)
                    if pcm:
                        self._feed_intercom_ref(target, pcm)
                except Exception:
                    pass
            return out
        except Exception as e:
            _logger.warning(f"[intercom][w3] relay compute 异常降级丢帧: {e}")
            return None

    def _buffer_grace_frame(self, sid: str, from_device_id: str, frame: bytes):
        """grace 期缓冲一帧(对端短暂断连), 待 on_reconnect 恢复(established)时由 _flush_grace 补齐。

        不在此处转码——对端(目标)此时已断连, 其协商的 phone_audio_codec 不可靠;
        留到 _flush_grace 重放时由原 relay 路径用恢复后的对端 conn 正常转码转发。

        注意：本方法总在调用方 relay() 已持 self._lock(threading.Lock, 非重入) 时调用,
        绝不可再取同一把锁, 否则同一线程自持自取 → 死锁事件循环(整进程 WS/对讲/events 冻死)。
        """
        buf = self._grace_buffers.get(sid)
        if buf is None:
            buf = collections.deque(maxlen=GRACE_BUFFER_MAX)
            self._grace_buffers[sid] = buf
        buf.append((from_device_id, frame))
        METRICS.inc("fuguoai_intercom_relay_buffered_total", 1.0,
                    labels={"from": from_device_id},
                    help_text="Intercom relay grace 期缓冲帧计数(待重连补齐, 非丢弃)")

    async def _flush_grace(self, sid: str):
        """grace 内重连恢复(established)后, 把缓冲帧按原 relay 路径重放(对端已在线, 正常转码转发)。"""
        frames = []
        with self._lock:
            buf = self._grace_buffers.pop(sid, None)
            if buf:
                frames = list(buf)
        if not frames:
            return
        _logger.info(f"[intercom][debug] _flush_grace: sid={sid} 补齐 {len(frames)} 帧")
        for from_device_id, frame in frames:
            try:
                await self.relay(from_device_id, frame)
            except Exception as e:  # 单帧失败不影响其余补齐
                _logger.warning(f"[intercom][debug] _flush_grace 重放失败: {e}")

    def _relay_debug_bytes(self, src: str, length: int, frame: bytes = None):
        """临时 debug：统计各来源到达 relay 的二进制帧，每 ~2s 打印一次，并带
        注册态/会话态/帧头 hex，用于定位「无声」（哪端没发帧 / 发了但被丢弃 / 帧格式错）。
        head=frame[:8].hex() 可肉眼判断设备帧格式（自 2026-08-01 起为裸 opus，无 seq 头）。"""
        # 快照注册态/会话态（单独取锁，不阻塞热路径日志）
        with self._lock:
            sid = self._by_device.get(src)
            st = self._sessions.get(sid, {}).get("state") if sid else None
        reg = sid is not None
        head = frame[:8].hex() if frame else ""
        stats = getattr(self, "_relay_stats", None)
        if stats is None:
            stats = {}
            self._relay_stats = stats
        s = stats.get(src)
        if s is None:
            s = {"bytes": 0, "frames": 0, "t": time.time()}
            stats[src] = s
        s["bytes"] += length
        s["frames"] += 1
        now = time.time()
        if now - s["t"] >= 2.0:
            _logger.info(
                f"[intercom][debug] relay到达: {src} len={length} head={head} "
                f"registered={reg} state={st}"
            )
            s["bytes"] = s["frames"] = 0
            s["t"] = now

    def _timing_note(self, phone_id: str, n_chunks: int):
        """flag 触发：统计手机→设备下行转发速率，证明网关层零堆积。

        n_chunks = 本帧拆出的 640B PCM 块数（每块=20ms opus）。每窗口打印
        转发音频时长 / 墙钟时长 之比：≈1.0 即网关逐帧实时转发（无队列）；
        >>1 说明上游手机侧突发把音频一次性灌入，网关只是即时透传。
        """
        d = getattr(self, "_timing", None)
        if d is None:
            d = {}
            self._timing = d
        st = d.get(phone_id)
        if st is None:
            st = {"audio": 0.0, "t": time.time(), "n": 0}
            d[phone_id] = st
        st["audio"] += 0.02 * max(n_chunks, 1)
        st["n"] += 1
        now = time.time()
        if now - st["t"] >= 2.0:
            wall = now - st["t"]
            _logger.info(
                f"[intercom][timing] 手机→设备下行 窗口={wall:.1f}s "
                f"转发音频={st['audio']:.1f}s 帧数={st['n']} "
                f"实时率={st['audio'] / wall:.2f} "
                f"(≈1.0=网关零堆积; >>1=上游突发)"
            )
            st["audio"] = 0.0
            st["n"] = 0
            st["t"] = now

    def is_in_session(self, device_id: str) -> bool:
        """临时诊断用：设备是否在某 intercom 会话中（含 grace 期）。"""
        with self._lock:
            return device_id in self._by_device

    def last_frame_age(self, device_id: str):
        """返回某端最近一次经 relay 成功转发(发出)距现在的秒数。

        用于首连自举判定「设备上行是否存活」：返回 None 表示该端从未发过帧
        (音频通道大概率未开)；返回大值表示该端长时间静默。线程安全。
        """
        with self._lock:
            ts = self._last_frame_ts.get(device_id)
        if ts is None:
            return None
        return time.time() - ts

    # ────────────────────────── 方案 B：phone 会话 opus<->PCM(可选 AAC) 转码 ──────────────────────────
    #
    # 设备侧固定走裸 opus（自 2026-08-01 起无 4B seq 头）；小程序 phone 侧编码由 hello 的 audio_codec 协商：
    # - 默认 pcm：relay→手机下行即 640B 裸 PCM，手机→relay 上行即 640B 裸 PCM（兼容旧小程序手写帧）。
    # - "aac"：relay→手机下行经 AacTranscoder 编码为 ADTS AAC，手机→设备上行从 ADTS AAC 解 PCM
    #   再经 opuslib_next 编码 opus。AAC 路径见 07-26 复盘（微信原生 AAC 而非手写 PCM）。
    # relay 仅对"一端为 phone、一端为设备"的会话做双向转码；设备↔设备(M2/loopback)零转码原样透传。

    def _get_phone_codec(self, phone_id: str, mode: str = "pcm"):
        """懒初始化并返回某 phone 会话的转码器（opus + 可选 AAC，有状态，须跨帧复用）。

        mode 由手机 hello 的 audio_codec 字段决定，会话内恒定（首次创建后锁定）：
        - "pcm"（默认，兼容旧小程序手写 640B 裸 PCM 帧）：仅建 opus 编解码，手机↔设备
          直接 PCM<->opus，relay→手机下行即 640B 裸 PCM，手机→relay 上行即 640B 裸 PCM。
        - "aac"（微信原生 AAC/ADTS，见 07-26 复盘）：额外建 AacTranscoder，设备→手机下行
          编码为 ADTS AAC，手机→设备上行从 ADTS AAC 解 PCM 再编码 opus。
        """
        with self._codec_lock:
            c = self._phone_codecs.get(phone_id)
            if c is None:
                enc = opuslib_next.Encoder(16000, 1, opuslib_next.APPLICATION_VOIP)
                try:
                    enc.bitrate = 24000  # 与设备低码率档接近，控制上行带宽
                except Exception:
                    pass
                if mode == "aac":
                    aac = AacTranscoder(16000)
                else:
                    aac = None
                c = {"enc": enc, "dec": opuslib_next.Decoder(16000, 1),
                     "aac": aac, "mode": mode}
                self._phone_codecs[phone_id] = c
                _logger.info(f"[intercom] phone 转码器已创建 phone_id={phone_id} mode={mode}")
            return c

    def _drop_phone_codec(self, device_id: str):
        """会话结束回收 phone 转码器（opus 状态 + ffmpeg 进程，不可泄漏）。"""
        if not device_id or not device_id.startswith("phone-"):
            return
        with self._codec_lock:
            c = self._phone_codecs.pop(device_id, None)
        if c and c.get("aac") is not None:
            try:
                c["aac"].close()
            except Exception:
                pass

    # ── B2 服务端 AEC（全双工）实例生命周期 ──────────────────────────

    def enable_intercom_aec(self, device_id: str, mode: str = "outband",
                             max_delay_ms: int = 2000, echo_gain_cap: float = 0.8,
                             echo_delay_ms: int = 0, echo_delay_band_ms: int = 0,
                             diag: bool = False,
                             multi_tap: bool = False, tap_len_ms: int = 120,
                             nlms_mu: float = 0.2, nlms_w_cap: float = 0.95,
                             dtd_ratio: float = 0.3,
                             residual_suppress: bool = True,
                             residual_floor: float = 0.1,
                             ner_outband: bool = False,
                             phone_align_corr_min: float = 0.3,
                             phone_misalign_floor: float = 0.05):
        """设备经 hello 协商开启服务端 AEC：创建/重置该设备的 AEC 实例。

        mode="2ch"    ：固件 aec_ready 后 2ch[mic|ref] 交织上行，互相关时延对齐谱减。
        mode="2ch_srvref"：仍走 B2 全双工(2ch 上行、mic 不静音)，但固件 ch1(ref) 实测
                    为空/坏→丢弃 ch1、取 ch0(mic)，回声参考改走服务端自喂(下行播放)，
                    互相关时延对齐谱减。适用本机固件 ref 通道不可用、但支持 2ch 全双工的板。
        mode="outband"：固件维持 mono 上行，ref 由后端自喂（下行 peer 音频解码）。
        max_delay_ms  ：outband 时延搜索窗口上限（90C0 深 DMA 缓冲须 >=8000）。
        echo_gain_cap：谱减 Wiener 增益上限（钳到声学回声增益，保近端），默认 0.8。
        echo_delay_ms ：长延迟设备(如 90C0 回声 5~9s)的回声延迟带中心(ms)；>0 时把
                    process_ref_cache 候选窗口收窄到 [echo_delay_ms - band, echo_delay_ms + band]，
                    彻底避免互相关在 12s 全窗口误选近期近端帧→回声未消→啸叫。默认 0=全窗口。
        echo_delay_band_ms：延迟带半宽(ms)；缺省 2000，保证窗口至少 4s 覆盖延迟波动。

        实例按 device_id 持有（opus 编解码 + 谱减状态有状态，须跨帧复用）；
        会话拆除/断连由 disable_intercom_aec 回收。
        """
        try:
            from fuguoai_core.utils.intercom_aec import IntercomAec
            # 自进化①：优先加载该设备真实回声路径指纹档案（来自上一通实测），
            # 自动覆盖人工默认 delay/band/gain——实测值优于凭记忆猜的写死值。
            # 人工 config 已显式传非零 echo_delay_ms 时尊重人工（保留覆盖能力）。
            _auto_delay, _auto_band, _auto_gain = echo_delay_ms, echo_delay_band_ms, echo_gain_cap
            _prof = None
            try:
                from fuguoai_core.utils.aec_profile_store import get_aec_profile_store
                _prof = get_aec_profile_store().maybe_load(device_id)
            except Exception:
                _prof = None
            if _prof:
                if int(echo_delay_ms) == 0:
                    _auto_delay = int(_prof.get("echo_delay_ms", 0))
                if int(echo_delay_band_ms) == 0:
                    _auto_band = int(_prof.get("echo_delay_band_ms", 0))
                _auto_gain = float(_prof.get("echo_gain_cap", echo_gain_cap))
            self._aec_instances[device_id] = IntercomAec(
                mode=mode, device_id=device_id, max_delay_ms=max_delay_ms,
                echo_gain_cap=_auto_gain,
                echo_delay_ms=_auto_delay, echo_delay_band_ms=_auto_band,
                diag=diag, multi_tap=multi_tap, tap_len_ms=tap_len_ms,
                nlms_mu=nlms_mu, nlms_w_cap=nlms_w_cap, dtd_ratio=dtd_ratio,
                residual_suppress=residual_suppress, residual_floor=residual_floor,
                ner_outband=ner_outband,
                phone_align_corr_min=phone_align_corr_min,
                phone_misalign_floor=phone_misalign_floor)
            self._aec_modes[device_id] = mode
            self._aec_max_delay[device_id] = max(2000, int(max_delay_ms))
            self._aec_echo_delay[device_id] = int(_auto_delay)
            self._aec_echo_band[device_id] = int(_auto_band)
        except Exception as e:
            _throttled_warn(f"aec_init:{device_id}",
                            f"[intercom] AEC 实例创建失败(降级不启用): {e}")

    def disable_intercom_aec(self, device_id: str):
        """会话结束/断连回收服务端 AEC 实例与关联状态（opus 状态不可泄漏）。"""
        # 诊断：会话结束输出回声延迟实测统计（仅 diag 开启且确有样本时）
        inst = self._aec_instances.get(device_id)
        if inst is not None and getattr(inst, "_diag_collected", False):
            try:
                _summary = inst.diag_summary()
                _logger.info(
                    f"[intercom][aec][diag] 回声延迟实测 device_id={device_id} "
                    f"mode={self._aec_modes.get(device_id)} -> {_summary}")
                # 自进化①：用本通实测诊断自动建档（覆盖/新建指纹档案）。
                # 仅在诊断开启时采集——避免生产常态开 diag 的内存开销；
                # diag 关时 diag_summary 样本为空，compute_profile 返回 None 不落盘。
                try:
                    from fuguoai_core.utils.aec_profile_store import get_aec_profile_store
                    get_aec_profile_store().maybe_record(device_id, _summary)
                except Exception as _e:
                    _logger.warning(f"[aec_profile] 建档失败 device={device_id} err={_e}")
            except Exception:
                pass
        self._aec_instances.pop(device_id, None)
        self._aec_modes.pop(device_id, None)
        self._aec_max_delay.pop(device_id, None)
        self._aec_echo_delay.pop(device_id, None)
        self._aec_echo_band.pop(device_id, None)
        self._ref_decoders.pop(device_id, None)
        self._intercom_ref.pop(device_id, None)
        self._intercom_ref_time.pop(device_id, None)

    def is_aec_enabled(self, device_id: str) -> bool:
        """该设备是否已协商开启服务端 AEC（供 hello 回显 aec_ready 判定）。"""
        return device_id in self._aec_instances

    def _get_ref_decoder(self, device_id: str):
        """懒建并返回某设备的自喂 ref 解码器（opus 16k mono，跨帧复用）。"""
        dec = self._ref_decoders.get(device_id)
        if dec is None:
            dec = opuslib_next.Decoder(16000, 1)
            self._ref_decoders[device_id] = dec
        return dec

    def _feed_intercom_ref(self, device_id: str, pcm: bytes):
        """后端自喂参考：把下行(→设备)解码出的 16k mono PCM 按服务端发送时刻存入 ref 缓存。

        关键修复：下行 opus 解码帧为 2880 样本(180ms)，而上行 mic 帧为 320 样本(20ms)，
        长度不等 → process_ref_cache 内 _td_corr 因 len 不等恒返 -1.0 → AEC 完全失效 →
        全双工(2ch_srvref)回声未消 → 啸叫。故此处把下行 PCM 切成与 mic 等长(320 样本)
        的子帧、ts 按 20ms 递增存入，保证互相关能正确匹配回声延迟。

        超出 (max_delay_ms + 余量) 的旧帧定期清理，避免缓存无限增长。
        """
        if not pcm:
            return
        samples = np.frombuffer(pcm, dtype=np.int16)
        n = samples.size
        if n == 0:
            return
        FR = 320  # 与上行 mic 帧等长(16k/20ms)
        base_ts = int(time.time() * 1000)
        d = self._intercom_ref.setdefault(device_id, {})
        t = self._intercom_ref_time.setdefault(device_id, {})
        n_sub = n // FR
        for i in range(n_sub):
            chunk = samples[i * FR:(i + 1) * FR].tobytes()
            ts = base_ts + i * 20  # 20ms/帧
            d[ts] = chunk
            t[ts] = time.time()
        cut = base_ts - (self._aec_max_delay.get(device_id, 8000) + 8000)
        for k in [k for k in d if k < cut]:
            d.pop(k, None)
            t.pop(k, None)

    def _intercom_aec_process(self, device_id: str, frame: bytes) -> bytes:
        """设备 uplink opus → server-side AEC → mono opus。

        仅当该设备已协商开启（_aec_instances 有实例）时生效；否则原样返回（B1/
        未协商，零开销零损伤）。失败降级返回原帧，绝不丢会话。
        outband 模式额外把后端自喂 ref 缓存 + 服务端到达时刻传入，供互相关时延对齐。
        """
        inst = self._aec_instances.get(device_id)
        if inst is None:
            return frame
        ref_cache = self._intercom_ref.get(device_id, {}) if inst.mode in ("outband", "2ch_srvref", "2ch") else None
        mic_ts = int(time.time() * 1000)
        out = inst.process_opus(frame, ref_cache, mic_ts)
        if out is None:
            _throttled_warn(f"aec_proc:{device_id}",
                            "[intercom] AEC 处理失败(降级原样透传)")
            return frame
        return out

    def _intercom_aec_process_phone(self, phone_id: str, pcm_frame: bytes) -> bytes:
        """手机 uplink 裸 PCM(640B/帧) → server-side AEC → PCM(逐帧谱减)。

        手机上行是 mono 640B PCM(pcm 模式)，无 ref 通道；回声参考由后端自喂：
        下行(→手机)已解码的 16k mono PCM 存入 _intercom_ref[phone_id](见 relay 下行分支)。
        AEC 在 _transcode 编码前对 PCM 逐 320 样本(640B)帧调 process_ref_cache 做互相关
        对齐谱减。仅当该手机已协商开启(实例存在)时生效；否则原样返回（零开销零损伤）。
        失败降级返回原帧，绝不丢会话。intercom_aec.py 零改动——复用 PCM 域 process_ref_cache。
        """
        inst = self._aec_instances.get(phone_id)
        if inst is None:
            return pcm_frame
        if len(pcm_frame) < 2 or len(pcm_frame) % 2 != 0:
            return pcm_frame
        n_chunks = len(pcm_frame) // 640
        if n_chunks == 0:
            return pcm_frame
        ref_cache = self._intercom_ref.get(phone_id, {}) if inst.mode in ("outband", "2ch_srvref") else None
        now_ms = int(time.time() * 1000)
        out = b""
        for i in range(n_chunks):
            off = i * 640
            chunk = pcm_frame[off:off + 640]
            # 帧尾≈now_ms，逐帧往前 -20ms 近似到达时刻（用于 ref 时延窗口对齐）
            ts = now_ms - (n_chunks - 1 - i) * 20
            try:
                cleaned = inst.process_ref_cache(chunk, ref_cache, ts)
            except Exception:
                cleaned = chunk
            out += cleaned if cleaned else chunk
        return out

    def _transcode(self, frame: bytes, from_device_id: str, target: str, phone_mode: str = "pcm"):
        """对 phone 会话做 opus<->PCM(可选 AAC) 双向转码。

        phone_mode 由手机 hello 的 audio_codec 决定（默认 pcm）：
        - pcm：设备→手机 下行=640B 裸 PCM；手机→设备 上行=640B 裸 PCM→opus。兼容旧小程序。
        - aac：设备→手机 下行=ADTS AAC(AacTranscoder)；手机→设备 上行=ADTS AAC→PCM→opus。
        同类型(设备↔设备 / 手机↔手机)原样透传。异常/缓冲不足返回 None(调用方丢帧)。
        """
        src_phone = from_device_id.startswith("phone-")
        dst_phone = target.startswith("phone-")
        if src_phone == dst_phone:
            get_stats_hub().note_frames(from_device_id, 1, len(frame))
            return frame  # 同类型，无需转码（设备↔设备 / 手机↔手机）
        try:
            if dst_phone:
                # 设备 → 手机
                if len(frame) < 4:
                    return None
                # 固件 2026-08-01 已移除 intercom 4 字节 seq 头：设备→手机下行收到裸 opus，
                # 不再剥 4B（剥了会吃掉真实 opus 首部 → 双端杂音）。直接透传给解码器。
                # 此处 <4B 仅作尺寸兜底（裸 opus 帧实际远 >4B，过短即视为损坏丢弃），非 seq 头校验。
                opus = frame
                # [诊断] 落盘设备下行原始裸 opus 帧(无 seq 头)，用于区分「解码参数错配」vs「帧头错位」
                try:
                    if os.path.exists("/opt/fuguoai-server/data/tmp/.intercom_rawdump"):
                        with open("/opt/fuguoai-server/data/tmp/intercom_downlink_raw.bin", "ab") as _rf:
                            _rf.write(struct.pack("<I", len(frame)) + frame)
                except Exception:
                    pass
                codec = self._get_phone_codec(target, phone_mode)
                try:
                    # 🔴 2026-08-13 FIX：设备 intercom 上行帧为 40~60ms@16k 裸 opus
                    # （固件复用 60ms 音频管线，TOC config 10/11 实测），原固定
                    # frame_size=320(20ms) 解码缓冲对 40/60ms 包返回
                    # OPUS_BUFFER_TOO_SMALL → 每帧丢弃 → 设备→手机 uplink 全失败 →
                    # last_frame_ts[device] 永不为更新 → watchdog 误判假在线强拆通话。
                    # 放宽到 2880(=60ms@48k 上限) 让解码器按包实际帧长输出，安全无副作用。
                    pcm = codec["dec"].decode(bytes(opus), 2880)
                except Exception as e:
                    get_stats_hub().note_err(from_device_id)
                    _throttled_warn(
                        f"tc_dec:{from_device_id}",
                        f"[intercom] 转码失败(设备→手机 解码, 丢帧): {e}",
                    )
                    return None
                get_stats_hub().note_frames(from_device_id, 1, len(pcm))
                if phone_mode == "aac":
                    aac_frames = codec["aac"].encode(pcm)
                    if not aac_frames:
                        return None
                    out_frame = b"".join(aac_frames)
                    dbg = f"aac_frames={len(aac_frames)} aac_len={len(out_frame)}"
                else:
                    out_frame = pcm
                    dbg = f"pcm_len={len(pcm)}"
                # ── 临时诊断：下行解码 PCM 落盘（flag 文件触发，联调抓波形判定削波/噪声用，稳定后删除）──
                try:
                    _DUMP_FLAG = "/opt/fuguoai-server/data/tmp/.intercom_dump"
                    if not os.path.exists(_DUMP_FLAG):
                        self._dump_open = False
                    elif dst_phone and phone_mode == "pcm":
                        _DUMP_PATH = "/opt/fuguoai-server/data/tmp/intercom_downlink.pcm"
                        _DUMP_CAP = 400  # ~8s @ 20ms/帧
                        if not getattr(self, "_dump_open", False):
                            with open(_DUMP_PATH, "wb") as _f:
                                _f.write(b"INTCPCM1")                     # magic
                                _f.write(struct.pack("<IHH", 16000, 1, 0))  # sr, ch, reserved
                            self._dump_open = True
                            self._dump_n = 0
                        _n = getattr(self, "_dump_n", 0)
                        if self._dump_open and _n < _DUMP_CAP:
                            with open(_DUMP_PATH, "ab") as _f:
                                _f.write(pcm)
                            self._dump_n = _n + 1
                except Exception:
                    pass
                # ── 临时诊断：打印设备→手机每帧格式（联调用，稳定后回退）──
                try:
                    d = getattr(self, "_dbg", None)
                    if d is None:
                        d = {}
                        self._dbg = d
                    k = "dec_" + target
                    st = d.get(k)
                    if st is None:
                        # printed 独立于 n 累加：原实现打印后把 n 归零，导致
                        # "前3条"条件恒成立 → 每帧都打（单通刷屏主因之一，08-01 修）
                        st = {"n": 0, "printed": 0, "t": 0.0}
                        d[k] = st
                    st["n"] += 1
                    now = time.time()
                    if st["printed"] < 3 or now - st["t"] >= 2.0:
                        _logger.info(
                            f"[intercom][debug] 设备→手机[{phone_mode}] {target} "
                            f"opus_len={len(opus)} pcm_len={len(pcm)} {dbg} "
                            f"frames={st['n']}"
                        )
                        st["printed"] += 1
                        st["n"] = 0
                        st["t"] = now
                except Exception:
                    pass
                return out_frame
            else:
                # 手机 → 设备
                if len(frame) < 2:
                    return None
                codec = self._get_phone_codec(from_device_id, phone_mode)
                if phone_mode == "aac":
                    pcm_chunks = codec["aac"].decode(frame)
                    if not pcm_chunks:
                        return None
                else:
                    # pcm 模式：上行帧即裸 PCM，按 640B 切块（尾料丢弃，opus 仅吃 320 样本）
                    pcm_chunks = [frame[o:o + 640]
                                  for o in range(0, len(frame) - len(frame) % 640, 640)]
                    if not pcm_chunks:
                        return None
                out = b""
                n20 = 0  # 本次实际发射的 20ms 帧数（timing 统计用）
                for pcm in pcm_chunks:
                    if len(pcm) < 2 or len(pcm) % 2 != 0:
                        continue
                    try:
                        op = codec["enc"].encode(bytes(pcm), len(pcm) // 2)
                    except Exception as e:
                        get_stats_hub().note_err(from_device_id)
                        _throttled_warn(
                            f"tc_enc:{from_device_id}",
                            f"[intercom] 转码失败(手机→设备 编码, 丢帧): {e}",
                        )
                        continue
                    # 固件 2026-08-01 已移除 intercom 4 字节 seq 头：手机→设备上行发裸 opus，
                    # 不再拼 seq（拼了设备解码器会收到带脏头的 opus → 双端杂音）。
                    out += op
                    n20 += 1
                if not out:
                    return None
                get_stats_hub().note_frames(from_device_id, n20, len(out))
                # [诊断] 落盘手机→设备原始裸 opus 帧(无 seq 头)，与设备→手机 `.intercom_rawdump`
                # 对称。下次会议离线 opuslib 解码算 RMS，判定 Bug2 是否我编码器产出：
                # 解码 RMS 与手机上行 RMS(0.002~0.20) 吻合=我方编码干净→噪音归固件；
                # 解码即噪音=我编码器坏，我方修。
                try:
                    if os.path.exists("/opt/fuguoai-server/data/tmp/.intercom_rawdump"):
                        with open("/opt/fuguoai-server/data/tmp/intercom_uplink_raw.bin", "ab") as _uf:
                            _uf.write(struct.pack("<I", len(out)) + out)
                except Exception:
                    pass
                # ── 临时诊断：手机→设备下行实时率（flag `.intercom_timing` 触发）──
                # 目的：证明网关层零堆积。实时率≈1.0=网关逐帧实时转发；
                # 若 >>1（如 2s 墙钟转发 60s 音频）则说明上游手机侧突发，而非网关排队。
                try:
                    if os.path.exists("/opt/fuguoai-server/data/tmp/.intercom_timing"):
                        # 按实际发射的 20ms 帧数计（不按 pcm_chunks，避免编码失败丢帧时高报）
                        self._timing_note(from_device_id, n20)
                except Exception:
                    pass
                # ── 临时诊断：打印手机→设备每帧格式（联调用，稳定后回退）──
                try:
                    d = getattr(self, "_dbg", None)
                    if d is None:
                        d = {}
                        self._dbg = d
                    k = "enc_" + from_device_id
                    st = d.get(k)
                    if st is None:
                        # 同上：printed 独立累加，修"前3条"恒成立导致的逐帧刷屏
                        st = {"n": 0, "printed": 0, "t": 0.0}
                        d[k] = st
                    st["n"] += 1
                    now = time.time()
                    if st["printed"] < 3 or now - st["t"] >= 2.0:
                        _logger.info(
                            f"[intercom][debug] 手机→设备[{phone_mode}] {from_device_id} "
                            f"in_len={len(frame)} pcm_chunks={len(pcm_chunks)} "
                            f"opus_out={len(out)} frames={st['n']}"
                        )
                        st["printed"] += 1
                        st["n"] = 0
                        st["t"] = now
                except Exception:
                    pass
                return out
        except Exception as e:
            _throttled_warn(
                f"tc_other:{from_device_id}",
                f"[intercom] 转码失败(丢帧): {e}",
            )
            return None

    # ────────────────────────── 断连兜底（grace） ──────────────────────────

    def on_disconnect(self, device_id: str):
        """WS 断开：不立即拆会话，进入 grace，起 GRACE_SEC 定时器。

        同步方法（从 _save_and_close 调用），定时器丢给运行中的事件循环。
        """
        with self._lock:
            if device_id in self._loopback:
                self._loopback.discard(device_id)
                return
            sid = self._by_device.get(device_id)
            if not sid:
                return
            rec = self._sessions.get(sid)
            if not rec:
                return
            rec["state"] = "grace"
            rec["grace_deadline"] = time.time() + GRACE_SEC
            rec["grace_device"] = device_id
            _logger.info(f"[intercom][debug] on_disconnect: {device_id} -> grace (sid={sid})")
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.get_event_loop()
            task = loop.create_task(self._grace_timer(device_id, sid))
            self._grace_tasks[device_id] = task
        except Exception:
            # 拿不到事件循环时直接拆除（保守兜底，不留悬挂会话）
            self._teardown_sync(device_id)

    def on_reconnect(self, device_id: str):
        """grace 内同 device_id 重连（hello session=intercom）→ 恢复会话, 并异步补齐 grace 期缓冲帧。"""
        restored_peer = None
        restored_sid = None
        with self._lock:
            sid = self._by_device.get(device_id)
            if sid:
                rec = self._sessions.get(sid)
                if rec and rec["state"] == "grace" and rec.get("grace_device") == device_id:
                    rec["state"] = "established"
                    rec["grace_deadline"] = 0.0
                    rec["grace_device"] = None
                    restored_peer = rec["b"] if rec["a"] == device_id else rec["a"]
                    restored_sid = sid
        self._cancel_grace(device_id)
        if restored_peer:
            _logger.info(f"[intercom][debug] on_reconnect: {device_id} 恢复会话 peer={restored_peer}")
            # 对端已在线: 异步重放 grace 期缓冲帧(正常转码转发), 避免音频成片丢失。
            try:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = asyncio.get_event_loop()
                loop.create_task(self._flush_grace(restored_sid))
            except Exception:
                pass
        else:
            _logger.info(f"[intercom][debug] on_reconnect: {device_id} 无 grace 会话(视为新连接)")
        return restored_peer

    async def _grace_timer(self, device_id: str, sid: str):
        try:
            await asyncio.sleep(GRACE_SEC)
        except asyncio.CancelledError:
            return
        peer = None
        with self._lock:
            rec = self._sessions.get(sid)
            if not rec or rec["state"] != "grace" or rec.get("grace_device") != device_id:
                return  # 已恢复或已拆除
            self._sessions.pop(sid, None)
            self._grace_buffers.pop(sid, None)  # grace 超时未恢复: 缓冲帧丢弃(不补齐)
            self._by_device.pop(rec["a"], None)
            self._by_device.pop(rec["b"], None)
            self._drop_phone_codec(rec["a"])
            self._drop_phone_codec(rec["b"])
            self.disable_intercom_aec(rec["a"])  # 会话彻底拆除: 回收 AEC
            self.disable_intercom_aec(rec["b"])
            peer = rec["b"] if rec["a"] == device_id else rec["a"]
        get_stats_hub().session_end(sid, "grace_timeout")
        self._grace_tasks.pop(device_id, None)
        if peer:
            await _send_event(peer, "peer_left", device_id)

    def _cancel_grace(self, device_id: str):
        task = self._grace_tasks.pop(device_id, None)
        if task and not task.done():
            task.cancel()

    def _teardown_sync(self, device_id: str):
        with self._lock:
            sid = self._by_device.pop(device_id, None)
            if sid:
                rec = self._sessions.pop(sid, None)
                self._grace_buffers.pop(sid, None)  # 同步拆除: 缓冲帧丢弃
                if rec:
                    self._by_device.pop(rec["a"], None)
                    self._by_device.pop(rec["b"], None)
                    self._drop_phone_codec(rec["a"])
                    self._drop_phone_codec(rec["b"])
                    self.disable_intercom_aec(rec["a"])  # 同步拆除: 回收 AEC
                    self.disable_intercom_aec(rec["b"])
        if sid:
            get_stats_hub().session_end(sid, "teardown")

    # ────────────────────────── 查询 ──────────────────────────

    def peer_of(self, device_id: str):
        with self._lock:
            if device_id in self._loopback:
                return "loopback"
            sid = self._by_device.get(device_id)
            if not sid:
                return None
            rec = self._sessions.get(sid)
            if not rec:
                return None
            return rec["b"] if rec["a"] == device_id else rec["a"]

    def snapshot(self):
        with self._lock:
            return {
                "sessions": {
                    sid: {k: v for k, v in rec.items()}
                    for sid, rec in self._sessions.items()
                },
                "loopback": sorted(self._loopback),
            }


_REGISTRY = None


def get_intercom_registry() -> IntercomRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = IntercomRegistry()
    return _REGISTRY
