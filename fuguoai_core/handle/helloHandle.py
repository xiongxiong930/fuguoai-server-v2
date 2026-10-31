"""hello 握手 / 对讲注册 / 唤醒词回复编排链（复果原生实现）。

处理设备（与小程序 phone 端）建连后的 hello 握手：协商音频参数、特性、对讲（intercom）
透传模式、服务端 AEC，并完成 B-5 手机端 JWT 认证注册。本协议契约由后端定义，固件侧对齐此实现。

其中「对讲 intercom 透传识别」「B-5 手机端 JWT 派生 phone-<user_id> 注册」「GREET
sample_rate 回显修正（2026-08-22 杂音根因）」「B2 全双工服务端 AEC 协商（含板型 profile
权威源 + 灰度名单回退 outband）」「grace 内重连恢复下行路由」均为 fuguoai 自有演进，
注释保留作为排障依据。唤醒词回复生成/缓存为上游骨架，已在下方以 fuguoai 口径重表达。
"""

import time
import json
import uuid
import random
import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler
from fuguoai_core.utils.dialogue import Message
from fuguoai_core.utils.util import audio_to_data
from fuguoai_core.providers.tts.dto.dto import SentenceType
from fuguoai_core.utils.wakeup_word import WakeupWordsConfig
from fuguoai_core.handle.sendAudioHandle import sendAudioMessage, send_tts_message
from fuguoai_core.utils.util import remove_punctuation_and_length, opus_datas_to_wav_bytes, normalize_device_id
from fuguoai_core.providers.tools.device_mcp import MCPClient, send_mcp_initialize_message
import os
from fuguoai_config.paths import ASSETS_DIR

TAG = __name__

# 唤醒词回复语料（复果自有文案；与固件唤醒命中集合对齐的是 config.wakeup_words）
WAKEUP_REPLY_BANK = {
    "refresh_seconds": 10,
    "phrases": [
        "我一直都在呢，您请说。",
        "在的呢，请随时吩咐我。",
        "来啦来啦，请告诉我吧。",
        "您请说，我正听着。",
        "请您讲话，我准备好了。",
        "请您说出指令吧。",
        "我认真听着呢，请讲。",
        "请问您需要什么帮助？",
        "我在这里，等候您的指令。",
    ],
}

# 全局唤醒词配置管理器
wakeup_words_config = WakeupWordsConfig()

# 防并发刷新唤醒词回复的锁
_wakeup_refresh_lock = asyncio.Lock()


def _is_intercom_session(conn: "ConnectionHandler", msg_json: dict) -> bool:
    """识别对讲（intercom）透传连接。

    优先用 hello.session=="intercom"；旧固件持久化复用通道不重发该字段时，
    回退到 intercom_registry.is_in_session(device_id)（与 connection.py 的
    _should_relay 语义一致），避免把对讲误判成普通连接导致 GREET 码率错配杂音。
    """
    is_intercom = (msg_json.get("session") == "intercom")
    if not is_intercom:
        try:
            from fuguoai_core.utils.intercom_registry import get_intercom_registry
            is_intercom = get_intercom_registry().is_in_session(conn.device_id)
        except Exception:
            pass
    return is_intercom


def _echo_greet_audio_params(conn: "ConnectionHandler", audio_params: dict) -> dict:
    """构造 GREET 回显的 audio_params。

    对讲(intercom)：保持设备上报原值（下行是 16k 裸 opus 透传，须保持回声 16000）。
    普通连接：回显后端真实推流码率 conn.sample_rate（与 tts/base.py 推流口径一致，
    否则设备建 16k 解码器解 24k opus → 全杂音，2026-08-22 根因修复）。
    """
    if getattr(conn, "intercom_mode", False):
        return audio_params
    fixed = dict(audio_params)  # 浅拷贝，不污染原始
    fixed["sample_rate"] = conn.sample_rate
    return fixed


def _pick_phone_codec(msg_json: dict) -> str:
    """协商手机↔relay 音频编码：默认 pcm，仅接受 pcm/aac，其它一律回退 pcm。"""
    codec = msg_json.get("audio_codec", "pcm")
    if codec not in ("pcm", "aac"):
        codec = "pcm"
    return codec


def _collect_aec_common(icfg: dict, device_id: str) -> dict:
    """抽取服务端 AEC 的公共参数（设备腿与手机腿共用部分）。"""
    diag_dev = str(icfg.get("intercom_aec_diag_device", "") or "")
    return dict(
        max_delay_ms=int(icfg.get("aec_max_delay_ms", 2000)),
        echo_gain_cap=float(icfg.get("aec_echo_gain_cap", 0.8)),
        echo_delay_ms=int(icfg.get("aec_echo_delay_ms", 0)),
        echo_delay_band_ms=int(icfg.get("aec_echo_delay_band_ms", 0)),
        diag=bool(icfg.get("intercom_aec_diag")) and (not diag_dev or diag_dev == device_id),
        multi_tap=bool(icfg.get("aec_multi_tap", False)),
        tap_len_ms=int(icfg.get("aec_tap_len_ms", 120)),
        nlms_mu=float(icfg.get("aec_nlms_mu", 0.2)),
        nlms_w_cap=float(icfg.get("aec_nlms_w_cap", 0.95)),
        dtd_ratio=float(icfg.get("aec_dtd_ratio", 0.3)),
    )


def _arm_phone_server_aec(conn: "ConnectionHandler", icfg: dict) -> None:
    """手机双端 AEC：outband 模式（上行裸 PCM 逐帧谱减，回声参考=下行自喂）。

    不注入 aec_ready（手机非 1ch 板、本就 mono，注入无意义且固件侧不识别）。
    server_aec 开启即自动给对端手机做服务端 AEC，与设备端对称 → 双向破跨设备声学环。
    """
    try:
        from fuguoai_core.utils.intercom_registry import get_intercom_registry
        common = _collect_aec_common(icfg, conn.device_id)
        # 手机腿专用回声增益上限：声学耦合强，需独立高 cap 让线性 AEC 在源头取真实增益，
        # 否则被钳 0.8 仅抵消 ~0.8dB，剩余回声只靠 NER 地板压 → 净环路增益>1 → 手机啸叫。
        # 覆盖 common 中的默认 0.8（避免与 **common 展开重复同名关键字）。
        common["echo_gain_cap"] = float(icfg.get("aec_phone_echo_gain_cap", 10.0))
        phone_rf = float(icfg.get("aec_phone_residual_floor", 0.1))
        phone_ner = bool(icfg.get("aec_phone_residual_suppress", True))
        # 手机腿对齐置信门控：best_corr 低于阈值视为对齐失败→跳过线性谱减→改由
        # misalign_floor 强抑制地板断环，避免误削近端/误留回声。
        phone_align_corr_min = float(icfg.get("aec_phone_align_corr_min", 0.3))
        phone_misalign_floor = float(icfg.get("aec_phone_misalign_floor", 0.05))
        get_intercom_registry().enable_intercom_aec(
            conn.device_id, mode="outband", residual_floor=phone_rf, ner_outband=phone_ner,
            phone_align_corr_min=phone_align_corr_min,
            phone_misalign_floor=phone_misalign_floor, **common)
        conn._intercom_aec_mode = "outband"
        conn.logger.bind(tag=TAG).info(
            f"[intercom][aec] 服务端 AEC(双端)已启用 phone_id={conn.device_id} mode=outband")
    except Exception as e:
        conn.logger.bind(tag=TAG).warning(f"[intercom][aec] phone 启用失败(已忽略): {e}")


def _arm_device_server_aec(conn: "ConnectionHandler", icfg: dict) -> None:
    """设备端服务端 AEC：板型 profile 为 per-board aec.mode 权威源；灰度名单回退 outband。

    仅 2ch 模式才向固件下发 aec_ready（outband 1ch 板维持 mono，注入会令 mono 被当 2ch 解码→损坏）。
    """
    try:
        from fuguoai_core.utils.intercom_registry import get_intercom_registry
        from fuguoai_core.device_profiles import resolve_board_profile
        profile = resolve_board_profile(conn.device_id)
        mode = profile.aec.mode or str(icfg.get("server_aec_mode", "outband"))
        allow = icfg.get("server_aec_devices") or []
        if allow and conn.device_id not in allow and profile.aec.mode is None:
            mode = "outband"
            conn.logger.bind(tag=TAG).info(
                f"[intercom][aec] device {conn.device_id} 不在 server_aec_devices "
                f"灰度名单，回退 outband(1ch mono+后端自喂 ref)")
        common = _collect_aec_common(icfg, conn.device_id)
        # NER 残差压制：接上 config 的 aec_residual_suppress / aec_residual_floor，
        # 默认 True/0.1 与旧行为完全等价（零回归）。
        res_supp = bool(icfg.get("aec_residual_suppress", True))
        res_floor = float(icfg.get("aec_residual_floor", 0.1))
        get_intercom_registry().enable_intercom_aec(
            conn.device_id, mode=mode, residual_suppress=res_supp,
            residual_floor=res_floor, **common)
        conn._intercom_aec_mode = mode
        conn.logger.bind(tag=TAG).info(
            f"[intercom][aec] 服务端 AEC 已启用 device_id={conn.device_id} mode={mode}")
    except Exception as e:
        conn.logger.bind(tag=TAG).warning(f"[intercom][aec] 启用失败(已忽略): {e}")


def _merge_aec_ready_into_welcome(conn: "ConnectionHandler") -> dict:
    """若已启用服务端 AEC 且非 outband 模式，向 welcome 浅拷贝副本注入 aec_ready/server_aec。

    outband(1ch 板) 绝不注入——固件会切 2ch 上行 → mono 被当 2ch 解码 → 音频损坏。
    """
    welcome = conn.welcome_msg
    try:
        from fuguoai_core.utils.intercom_registry import get_intercom_registry
        if         get_intercom_registry().is_aec_enabled(conn.device_id):
            aec_mode = getattr(conn, "_intercom_aec_mode", "2ch")
            if aec_mode != "outband":
                welcome = dict(conn.welcome_msg)  # 浅拷贝：不改动嵌套共享 dict
                welcome["aec_ready"] = True
                welcome["server_aec"] = True
    except Exception:
        pass
    return welcome


def _build_v30_welcome(conn: "ConnectionHandler") -> dict:
    """构造 v3.0 welcome（不污染共享 config["fuguoai"]）。

    v3.0 用显式字段回报服务端推流参数，替代 legacy 的 audio_params 整块回显：
      - type:"welcome"（与 legacy 的 type 区分，固件据此切换解析分支）
      - session_id：会话关联（固件原样回传）
      - server_sample_rate / server_frame_duration / server_audio_format：
        服务端下发 opus 的真实采样率/帧时长(ms)/格式
      - aec_ready / server_aec：非 outband 模式才注入（2ch 上行提示）
      - protocol_version：便于固件日志/调试
    """
    fuguoai_cfg = (conn.config.get("fuguoai", {}) or {})
    frame_duration = int(
        (fuguoai_cfg.get("audio_params", {}) or {}).get("frame_duration", 60)
    )
    welcome = {
        "type": "welcome",
        "session_id": conn.session_id,
        "protocol_version": "3.0.0",
        "server_sample_rate": conn.sample_rate,
        "server_frame_duration": frame_duration,
        "server_audio_format": conn.audio_format or "opus",
    }
    # AEC：与 legacy 一致，仅非 outband 模式注入 aec_ready（2ch 上行提示）
    try:
        from fuguoai_core.utils.intercom_registry import get_intercom_registry

        if get_intercom_registry().is_aec_enabled(conn.device_id):
            aec_mode = getattr(conn, "_intercom_aec_mode", "2ch")
            if aec_mode != "outband":
                welcome["aec_ready"] = True
                welcome["server_aec"] = True
    except Exception:
        pass
    # 回显设备上报的 capabilities，供固件校验协商结果
    caps = getattr(conn, "client_capabilities", None)
    if caps:
        welcome["client_capabilities"] = caps
    return welcome


async def _phone_login_and_register(conn: "ConnectionHandler", msg_json) -> bool:
    """B-5.1 · 小程序真人语音对讲（模式 B）phone 端认证 + 在线注册（契约 v1.9）。

    与设备不同，手机端不带 device-id 头连接 → connection.py 在建连时因
    device_id 为空跳过了 mark_online/register_* 全部注册。故 phone 端必须在
    hello 内用 **用户 JWT** 完成认证并把自己注册进 connection_registry，之后
    _get_conn / relay / is_online 才对手机端生效。

    身份规则（后端派生，手机不自造）：
      user_id ← verify_token(hello.token)
      conn.device_id = "phone-" + user_id   （不过 normalize：保留连字符，
          与全 hex 的设备 MAC 天然不撞；registry 仅当作不透明 key 使用）

    向后兼容：仅当 hello 带 role=="phone" 才进入本分支；设备(无 role)零影响。
    返回 True=认证并注册成功；False=认证失败(调用方负责回错并关连接)。
    """
    from fuguoai_core.utils.user_auth import verify_token

    token = msg_json.get("token")
    payload = verify_token(token) if token else None
    if not payload or not payload.get("user_id"):
        conn.logger.bind(tag=TAG).warning(
            "[intercom][phone] 认证失败：token 缺失/非法/过期，拒绝手机端 hello"
        )
        return False

    user_id = str(payload["user_id"])
    phone_id = "phone-" + user_id  # 逻辑身份，勿归一化（会吃掉连字符）
    target = normalize_device_id(msg_json.get("target_device_id"))

    # 防御：若建连时已按某 header device-id 注册过（正常手机端不会），先摘除旧登记
    old_id = getattr(conn, "device_id", None)
    if old_id and old_id != phone_id:
        try:
            from fuguoai_core.utils.connection_registry import get_connection_registry
            _reg = get_connection_registry()
            _uid = getattr(conn, "conn_uid", None)
            _reg.mark_offline(old_id, conn.session_id, conn_uid=_uid)
            _reg.unregister_kick(old_id, conn.session_id, conn_uid=_uid)
            _reg.unregister_send(old_id, conn.session_id, conn_uid=_uid)
        except Exception as e:
            conn.logger.bind(tag=TAG).warning(f"[intercom][phone] 摘除旧登记异常(已忽略): {e}")

    conn.device_id = phone_id
    conn.is_phone = True
    conn.user_id = user_id
    # 供 B-5.2/B-5.3 内联发起使用（本期仅记录，不自动发起）
    conn.intercom_target = target
    conn.logger = conn.logger.bind(device_id=phone_id)

    # 注册进 connection_registry（镜像 connection.py 建连时对设备做的登记），
    # 使对讲透传的 _get_conn(phone_id) / is_online(phone_id) 生效。
    try:
        from fuguoai_core.utils.connection_registry import get_connection_registry
        reg = get_connection_registry()
        _cuid = getattr(conn, "conn_uid", None)
        reg.mark_online(phone_id, conn.session_id, getattr(conn, "client_ip", None),
                        conn_uid=_cuid)
        # 踢连接：惰性读取 websocket（关闭帧即可，finally 会走完整清理）
        reg.register_kick(phone_id, conn.session_id, lambda: conn.websocket.close(),
                          conn_uid=_cuid)
        # 下发目标：返回 connection 本身，send_to_device / relay 经 safe_send* 串行下发
        reg.register_send(phone_id, conn.session_id, lambda: conn, conn_uid=_cuid)
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"[intercom][phone] 在线注册异常: {e}")
        return False

    conn.logger.bind(tag=TAG).info(
        f"[intercom][phone] 认证成功 user_id={user_id} phone_id={phone_id} "
        f"target={target or '(未指定)'}"
    )
    return True


async def handleHelloMessage(conn: "ConnectionHandler", msg_json):
    """处理hello消息"""
    # P0-3：标记已完成合法 hello，解除握手超时判定
    conn._hello_received = True

    # ── 对讲（Intercom）模式识别（契约 v1.6.0 Part B）──
    conn.intercom_mode = _is_intercom_session(conn, msg_json)

    # ── B-5.1 · 小程序真人语音对讲（模式 B）phone 端认证分支（契约 v1.9）──
    # 手机端 hello 带 role:"phone" + token(用户JWT) + target_device_id，
    # 不带 device-id 头。此处用 JWT 派生 phone-<user_id> 身份并注册在线，
    # 之后走与设备完全相同的 intercom 透传链路。设备(无 role)不进此分支。
    if msg_json.get("role") == "phone":
        ok = await _phone_login_and_register(conn, msg_json)
        if not ok:
            try:
                await conn.safe_send(json.dumps(
                    {"type": "hello", "error": "auth_failed"}, ensure_ascii=False
                ))
            except Exception:
                pass
            try:
                await conn.websocket.close()
            except Exception:
                pass
            return
        # 认证通过后：phone 一律按 intercom 透传处理
        conn.intercom_mode = True
        # B-5.4 · 手机↔relay 音频编码协商（契约 api_contract_v1.9_phone_modeB）：
        # 默认 "pcm"（兼容旧小程序手写 640B 裸 PCM 帧）；小程序在 hello 带
        # "audio_codec":"aac" 时切换为微信原生 AAC/ADTS。会话内恒定。
        audio_codec = _pick_phone_codec(msg_json)
        conn.phone_audio_codec = audio_codec
        conn.logger.bind(tag=TAG).info(
            f"[intercom][phone] 音频编码协商 audio_codec={audio_codec}"
        )
        # B-5.3 · phone 上线后立即补偿接通其 pending 呼叫（消除与 POST /intercom/call
        # 的到达顺序竞态；小程序 WSS 立即连、不等 polling，故此处主动触发接通）。
        try:
            from fuguoai_core.utils.intercom_call_store import get_call_orchestrator
            await get_call_orchestrator().notify_online(conn.device_id)
        except Exception as e:
            conn.logger.bind(tag=TAG).warning(
                f"[intercom][phone] notify_online 异常(已忽略): {e}"
            )

    # 设备固件版本（可选上报，向后兼容：旧固件不发送 fw_version 也不影响）。
    fw_version = msg_json.get("fw_version")
    if fw_version:
        conn.fw_version = fw_version
        conn.logger.bind(tag=TAG).info(f"客户端固件版本: {fw_version}")

    # ── v3.0 双版本握手：识别协议版本 ──
    # 判定依据：显式 version 字段以 "3" 开头，或 hello 携带 v3.0 的 capabilities
    # （替代 legacy 的 audio_params）。两种都按 v3.0 处理，其余按 legacy。
    version = str(msg_json.get("version") or "").strip()
    capabilities = msg_json.get("capabilities")
    is_v30 = version.startswith("3") or (capabilities is not None)
    conn.protocol_version = "3.0.0" if is_v30 else "1.0"
    conn.logger.bind(tag=TAG).info(
        f"握手协议版本: {'v3.0' if is_v30 else 'legacy'} "
        f"(version={version or '-'}, has_capabilities={capabilities is not None})"
    )

    # v3.0 麦流 ts 头恒开（8B 小端 µs 同源 ts 头），与是否携带 capabilities 无关，
    # 保证「仅 version=3.x、不带任何协商字段」的极简 hello 也按 v3.0 走 ts 头。
    # （下方 negotiated 块内仍有等价置位，此处提前确保覆盖极简分支。）
    if is_v30:
        conn.mic_ts_header = True

    audio_params = msg_json.get("audio_params")
    negotiated = audio_params or capabilities
    if negotiated:
        fmt = negotiated.get("format")
        conn.logger.bind(tag=TAG).debug(f"客户端音频格式: {fmt}")
        conn.audio_format = fmt
        if audio_params:
            # legacy：回写 GREET audio_params（下行协商，2026-08-22 杂音根因修复）
            conn.welcome_msg["audio_params"] = _echo_greet_audio_params(conn, audio_params)
            if getattr(conn, "intercom_mode", False):
                conn.logger.bind(tag=TAG).info(
                    f"[greet-fix] 对讲连接 GREET 回声 audio_params sample_rate="
                    f"{audio_params.get('sample_rate')} (下行 16k 透传, 须保持)")
            else:
                conn.logger.bind(tag=TAG).info(
                    f"[greet-fix] 普通连接 GREET audio_params.sample_rate 由 "
                    f"{audio_params.get('sample_rate')} → {conn.sample_rate} (与推流一致)")
        else:
            # v3.0 capabilities：仅记录，下行用 server_sample_rate 字段回报
            conn.client_capabilities = capabilities
        # ts 头协商：v3.0 恒带 8B 小端 uint64 µs 同源 ts 头；legacy 按字段。
        # 注：对讲透传分支（下方）会强制关掉，因透传帧无 ts 头。
        if is_v30:
            conn.mic_ts_header = True
            conn.logger.bind(tag=TAG).info("v3.0 麦流 ts 头恒开(8B 小端 µs)")
        else:
            conn.mic_ts_header = bool(int(negotiated.get("mic_ts_header", 0) or 0))
            if conn.mic_ts_header:
                conn.logger.bind(tag=TAG).info("客户端协商 mic_ts_header=1 (v1.10.10+ 同源 ts 头)")

    features = msg_json.get("features")
    if features and not getattr(conn, "intercom_mode", False):
        conn.logger.bind(tag=TAG).debug(f"客户端特性: {features}")
        conn.features = features
        if features.get("mcp"):
            conn.logger.bind(tag=TAG).debug("客户端支持MCP")
            conn.mcp_client = MCPClient()
            # 发送初始化
            asyncio.create_task(send_mcp_initialize_message(conn))
        if features.get("aec"):
            conn.logger.bind(tag=TAG).debug("客户端启用了服务端AEC")
            conn.client_aec = True

    # ── 临时诊断：设备重连若未带 session:intercom 但旧 intercom 会话仍在(可能 grace 中)，
    #    记一笔——「设备 WS 抖动→用普通 hello 重连→把 intercom 会话顶掉」的嫌疑路径 ──
    try:
        if not getattr(conn, "intercom_mode", False):
            from fuguoai_core.utils.intercom_registry import get_intercom_registry
            if get_intercom_registry().is_in_session(conn.device_id):
                conn.logger.bind(tag=TAG).warning(
                    f"[intercom][debug] 设备 {conn.device_id} 重连但未带 session:intercom，"
                    f"旧 intercom 会话仍在(可能 grace 中，上行/下行被丢)"
                )
    except Exception:
        pass

    if getattr(conn, "intercom_mode", False):
        # ⚠️ 头号坑：intercom 透传帧为**裸 opus 净荷**（2026-08-02 起移除 4B seq 头，
        # 固件同步切裸 opus），与 v1.10.10 对话链路的 8B 小端 ts 头互斥。
        # 强制关掉 mic_ts_header，防止透传帧被误剥 8 字节导致 opus 解码失败。
        conn.mic_ts_header = False
        # 后端不掺和 AEC（对讲 AEC 在设备侧 NLMS），也不进服务端 AEC 路径。
        conn.client_aec = False
        # ── B2 全双工服务端 AEC（默认关闭，仅当 config 显式开启且为真实设备端时启用）──
        icfg = (conn.config.get("fuguoai", {}) or {}).get("intercom", {}) or {}
        if getattr(conn, "is_phone", False):
            if icfg.get("server_aec") and icfg.get("server_aec_phone", True):
                _arm_phone_server_aec(conn, icfg)
        else:
            if icfg.get("server_aec"):
                _arm_device_server_aec(conn, icfg)
        # grace 内重连 → 恢复既有会话（对端无感）
        restored = None
        try:
            from fuguoai_core.utils.intercom_registry import get_intercom_registry
            restored = get_intercom_registry().on_reconnect(conn.device_id)
        except Exception as e:
            conn.logger.bind(tag=TAG).warning(f"[intercom] 重连恢复检查失败(已忽略): {e}")
        conn.logger.bind(tag=TAG).info(
            f"[intercom] 连接进入对讲透传模式 device_id={conn.device_id} restored={restored}"
        )
        if restored:
            conn.logger.bind(tag=TAG).info(
                f"[intercom] grace 内重连，恢复会话 peer={restored}"
            )
            # 🔴 FIX: 重连后重建下行路由（intercom 下行 0 真凶）
            # call 起点 WS 断连会打断当时下发的 SetIntercom 音频通道；
            # on_reconnect 仅恢复 intercom_registry 状态，不会重新向设备下发
            # SetIntercom → 固件侧音频通道未重开 → 设备下行(phone→device)永久 0。
            # 故此处向重连后的活连接重发一次 SetIntercom{peer,start} 重建下行路由。
            # 仅对真实设备端生效（与 _try_connect 契约一致：phone 端不下发 SetIntercom）。
            if not getattr(conn, "is_phone", False):
                try:
                    from fuguoai_core.utils.connection_registry import get_connection_registry
                    from fuguoai_core.utils.intercom_call_store import _set_intercom_cmd
                    ok = await get_connection_registry().send_to_device(
                        conn.device_id, _set_intercom_cmd(restored, "start")
                    )
                    conn.logger.bind(tag=TAG).info(
                        f"[intercom] 重连后重发 SetIntercom start peer={restored} -> {ok}"
                    )
                except Exception as e:
                    conn.logger.bind(tag=TAG).warning(
                        f"[intercom] 重连重发 SetIntercom 失败(已忽略): {e}"
                    )

    # B2 全双工：若已为该设备启用服务端 AEC，向 welcome 注入 aec_ready。
    # 按协议版本下发不同 welcome 形状：
    #  - legacy：回显 config["fuguoai"]（含 audio_params），type 由配置决定；
    #  - v3.0：下发极简 welcome（type:"welcome" + server_sample_rate 等），不污染共享配置。
    if getattr(conn, "protocol_version", "1.0").startswith("3"):
        welcome = _build_v30_welcome(conn)
    else:
        welcome = _merge_aec_ready_into_welcome(conn)
    await conn.safe_send(json.dumps(welcome, ensure_ascii=False))


async def checkWakeupWords(conn: "ConnectionHandler", text):
    """唤醒词命中检测：命中则播报唤醒回复并视为新会话。

    契约（与固件对齐）：
    - 仅当 config.enable_wakeup_words_response_cache 开启且 TTS 已就绪才生效。
    - 文本去标点/长度后比对 config.wakeup_words 集合；未命中即返回 False。
    - 命中后：置 just_woken_up=True、重置 client_abort、生成新 sentence_id
      （确保流控器重置），以 FIRST→LAST 两段播报唤醒回复，并写入对话历史。
    """
    enable_wakeup_words_response_cache = conn.config[
        "enable_wakeup_words_response_cache"
    ]

    # 等 TTS 初始化（最多 3s），未就绪直接放弃检测
    start_time = time.time()
    while time.time() - start_time < 3:
        if conn.tts:
            break
        await asyncio.sleep(0.1)
    else:
        return False

    if not enable_wakeup_words_response_cache:
        return False

    _, filtered_text = remove_punctuation_and_length(text)
    if filtered_text not in conn.config.get("wakeup_words"):
        return False

    conn.just_woken_up = True
    await send_tts_message(conn, "start")

    # 取当前音色（缺失回退 default）
    voice = getattr(conn.tts, "voice", "default")
    if not voice:
        voice = "default"

    # 取唤醒词回复配置；缺失则用内置兜底（ASSETS_DIR/wakeup_words_short.wav + 固定语）
    response = wakeup_words_config.get_wakeup_response(voice)
    if not response or not response.get("file_path"):
        response = {
            "voice": "default",
            "file_path": os.path.join(ASSETS_DIR, "wakeup_words_short.wav"),
            "time": 0,
            "text": "我在这里哦！",
        }

    # 拉取音频数据并播报
    opus_packets = await audio_to_data(response.get("file_path"), use_cache=False)
    conn.client_abort = False

    # 唤醒回复当作新会话：换新 sentence_id 令流控器重置
    conn.sentence_id = str(uuid.uuid4().hex)

    conn.logger.bind(tag=TAG).info(f"播放唤醒词回复: {response.get('text')}")
    await sendAudioMessage(conn, SentenceType.FIRST, opus_packets, response.get("text"))
    await sendAudioMessage(conn, SentenceType.LAST, [], None)

    # 写入对话历史
    conn.dialogue.put(Message(role="assistant", content=response.get("text")))

    # 回复配置超时则异步刷新（受全局锁保护，避免并发重复生成）
    if time.time() - response.get("time", 0) > WAKEUP_REPLY_BANK["refresh_seconds"]:
        if not _wakeup_refresh_lock.locked():
            asyncio.create_task(wakeupWordsResponse(conn))
    return True


async def wakeupWordsResponse(conn: "ConnectionHandler"):
    """唤醒词回复后台刷新：合成随机回复文案并落盘缓存，供下次命中直接取用。

    契约：全程受 _wakeup_refresh_lock 保护；任一环节失败（无 TTS / 取锁失败 /
    合成空）即安全退出并释放锁，不影响主链路。
    """
    if not conn.tts:
        return

    try:
        # 取锁失败（已有刷新在跑）直接返回
        if not await _wakeup_refresh_lock.acquire():
            return

        # 从预定义回复语料中随机选一条
        result = random.choice(WAKEUP_REPLY_BANK["phrases"])
        if not result or len(result) == 0:
            return

        # 合成 TTS 音频（线程池，避免阻塞事件循环）
        tts_result = await asyncio.to_thread(conn.tts.to_tts, result)
        if not tts_result:
            return

        # 取当前音色，按连接采样率封成 wav 落盘
        voice = getattr(conn.tts, "voice", "default")
        wav_bytes = opus_datas_to_wav_bytes(tts_result, sample_rate=conn.sample_rate)
        file_path = wakeup_words_config.generate_file_path(voice)
        with open(file_path, "wb") as f:
            f.write(wav_bytes)
        # 更新配置缓存
        wakeup_words_config.update_wakeup_response(voice, file_path, result)
    finally:
        # 任何路径都必须释放锁
        if _wakeup_refresh_lock.locked():
            _wakeup_refresh_lock.release()
