"""barge-in 语义门控（Joy-Duplex 五状态思想轻量化落地，R1+R2）。

本模块只做减法（veto），绝不新增 fire。在 receiveAudioHandle.py 的纯声学
fire 决策点之后、实际 handleAbortMessage 之前插入，对"声学已判定可打断"的
帧做语义层裁决：

    ACCEPT      ：有效面向系统的输入 → 放行 fire（维持原行为）
    PARTIAL     ：意图展开中 → 放行（不拦截，避免漏打断）
    BACKCHANNEL ："嗯/对"等短反馈 → 拦截 fire（不接管话轮）
    REJECT      ：背景/非目标输入（电视/旁人声）→ 拦截 fire

映射到京东 JoyAI-Talker 的 Joy-Duplex 五状态（partial/complete/backchannel/
accept/reject）思想 —— 把"快反射"（声学 VAD，已存在）、"语义门控"（本模块）、
"精细执行"（LLM/TTS）按时间尺度解耦，而不是把轮次判断塞进声学回路。论文用
CRESPOND(打断响应)=0.88、背景误触=0.10 证明纯声学 VAD 在背景误触上必然吃亏，
必须有语义/说话人门。

R1（骨架，零行为变更）：
  - `bargein_semantic_gate_enabled=false`（默认）→ 恒 ACCEPT 全透传。
  - enabled=true 且 `bargein_reject_unknown_speaker=false` → 仍 ACCEPT（安静，
    零行为变更），仅占位 R2 路径。

R2（声纹 reject 层，本文件实现）：
  - `bargein_reject_unknown_speaker=true` 时启用。调用腾讯云 1:N 声纹识别
    （identify_speaker）区分"已注册家庭成员"与"未注册者（电视/旁人/未注册）"。
  - **非阻塞**：声纹是网络 API，绝不在实时音频帧处理路径里 await 它。改用
    asyncio.ensure_future 后台跑，fire 路径只查 per-conn 缓存的裁决状态。
  - **多票延迟裁决**：单次识别为"未知说话人"可能是噪声/短音频低置信度误判，
    需连续 N 次（默认 2）独立识别一致为未知才 REJECT，避免单帧噪声误拒主人。
  - **保守降级**：声纹未启用 / 无登记人 / API 异常 / 识别超时 → ACCEPT（不 reject）。
    仅在"系统已知至少一个家庭成员、且当前说话人持续匹配不到任何已注册者"时
    才 REJECT（对应电视/旁人持续声）。

安全网：任何异常一律降级为 ACCEPT（放行），语义门故障绝不阻断正常打断。
"""
import asyncio
import struct
import time
from enum import Enum


class BargeState(str, Enum):
    """barge-in 语义门控状态（对应 Joy-Duplex 五状态轻量化）。"""
    ACCEPT = "accept"
    PARTIAL = "partial"
    BACKCHANNEL = "backchannel"
    REJECT = "reject"


# 拦截 fire（不打断）的状态：TTS 继续播、话轮不交接
BLOCKING_STATES = frozenset({BargeState.REJECT, BargeState.BACKCHANNEL})

# ── R2 常量 ──
_GATE_ATTR = "_barge_semantic_gate"   # per-conn 状态挂在 conn 上的属性名
_BUF_SEC = 1.5                        # 音频累积上限（秒）
_MIN_SEC = 0.8                        # 触发识别的最小累积音频（秒）
_REID_INTERVAL = 0.9                  # 两次后台识别最小间隔（秒）
_UNKNOWN_VOTES_REJECT = 2             # 连续多少次"未知"才 REJECT（多票防误拒）
_SR = 16000                           # 采样率（声纹要求 16k）
_BYTES_PER_SEC = _SR * 2              # 16bit mono


def _gate_enabled(conn) -> bool:
    """读运行时配置（conn.config 扁平键）。默认 False = 全透传。"""
    try:
        _cfg = getattr(conn, "config", None)
        if not _cfg:
            return False
        return bool(_cfg.get("bargein_semantic_gate_enabled", False))
    except Exception:
        return False


def _reject_unknown_enabled(conn) -> bool:
    """R2 开关：是否启用"未注册说话人→REJECT"。默认 False（保守，零 reject）。"""
    try:
        _cfg = getattr(conn, "config", None)
        if not _cfg:
            return False
        return bool(_cfg.get("bargein_reject_unknown_speaker", False))
    except Exception:
        return False


def _ensure_gate(conn) -> dict:
    """取/建 per-conn 语义门状态字典。"""
    g = getattr(conn, _GATE_ATTR, None)
    if g is None:
        g = {
            "buf": [],            # list[bytes]，累积 int16 PCM @16k
            "state": "pending",   # pending | accepted | rejected
            "task": None,         # 后台识别 asyncio.Task
            "last_identify": 0.0,
            "unknown_votes": 0,
        }
        try:
            setattr(conn, _GATE_ATTR, g)
        except Exception:
            pass
    return g


def _buf_sec(buf) -> float:
    return sum(len(c) for c in buf) / _BYTES_PER_SEC


def _trim_buf(buf, max_sec: float) -> None:
    max_bytes = int(_BYTES_PER_SEC * max_sec)
    total = sum(len(c) for c in buf)
    while total > max_bytes and len(buf) > 1:
        removed = buf.pop(0)
        total -= len(removed)


def _pcm_to_wav(pcm: bytes, sample_rate: int = _SR) -> bytes:
    """把裸 int16 PCM bytes 包成 16k mono 16bit WAV（声纹 API 要求 wav）。"""
    n = len(pcm)
    header = (
        b"RIFF" + struct.pack("<I", 36 + n) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate,
                                sample_rate * 2, 2, 16)
        + b"data" + struct.pack("<I", n)
    )
    return header + pcm


def _warn(conn, msg: str) -> None:
    try:
        conn.logger.bind(tag="BARGE-GATE").warning(f"[BARGE-GATE] {msg}")
    except Exception:
        pass


def _info(conn, msg: str) -> None:
    try:
        conn.logger.bind(tag="BARGE-GATE").info(f"[BARGE-GATE] {msg}")
    except Exception:
        pass


async def _run_identify(conn, gate: dict) -> None:
    """后台异步声纹识别（非阻塞）。结果写入 gate['state']。"""
    try:
        from fuguoai_core.utils.voiceprint_provider import get_voiceprint_provider
        vp = get_voiceprint_provider(getattr(conn, "config", None) or {})
        if vp is None:
            # 声纹未启用 → 不 reject（保守）
            gate["state"] = "accepted"
            gate["unknown_votes"] = 0
            return
        wav = _pcm_to_wav(b"".join(gate["buf"]))
        dev = getattr(conn, "device_id", "unknown")
        name = await vp.identify_speaker(wav, dev)
        if name is None:
            # 无登记人 / 未启用 → 不 reject
            gate["state"] = "accepted"
            gate["unknown_votes"] = 0
        elif name == "未知说话人":
            gate["unknown_votes"] = gate.get("unknown_votes", 0) + 1
            if gate["unknown_votes"] >= _UNKNOWN_VOTES_REJECT:
                gate["state"] = "rejected"
                _info(conn, f"声纹多票未知→REJECT（votes={gate['unknown_votes']}）")
        else:
            # 识别到具体已注册家庭成员 → 授权，放行
            gate["state"] = "accepted"
            gate["unknown_votes"] = 0
    except Exception as e:
        gate["state"] = "accepted"  # 保守降级，绝不阻断打断
        _warn(conn, f"声纹识别异常降级 accepted: {e}")
    finally:
        gate["task"] = None


async def semantic_bargein_decide(conn, pcm_frame, raw_rms: float, base_rms: float) -> BargeState:
    """声学 fire-eligible 后的语义裁决（async，非阻塞）。

    返回状态决定 fire 是否放行（见 BLOCKING_STATES）。

    安全：任何异常降级为 ACCEPT（绝不阻断正常打断）。
    """
    try:
        if not _gate_enabled(conn):
            return BargeState.ACCEPT
        if not _reject_unknown_enabled(conn):
            # R1 灰度 / R2 未启用：安静返回 ACCEPT，零行为变更
            return BargeState.ACCEPT
        # ── R2 声纹 reject 层（非阻塞延迟裁决）──
        gate = _ensure_gate(conn)
        # 累积 pcm（bytes int16 @16k）
        try:
            chunk = pcm_frame if isinstance(pcm_frame, (bytes, bytearray)) else bytes(pcm_frame)
        except Exception:
            chunk = b""
        if chunk:
            gate["buf"].append(chunk)
            _trim_buf(gate["buf"], _BUF_SEC)
        # 已裁决为 rejected → 直接拦截（TTS 继续播、话轮不交接）
        if gate["state"] == "rejected":
            return BargeState.REJECT
        # 触发后台识别（ensure_future 不阻塞 fire 路径；上一轮完成才起下一轮）
        _now = time.monotonic()
        if (gate["task"] is None
                and (_now - gate["last_identify"]) >= _REID_INTERVAL
                and _buf_sec(gate["buf"]) >= _MIN_SEC):
            gate["last_identify"] = _now
            gate["task"] = asyncio.ensure_future(_run_identify(conn, gate))
        # pending / accepted / 首次：保守放行（绝不阻断真人打断）
        return BargeState.ACCEPT
    except Exception as e:
        _warn(conn, f"裁决异常降级 ACCEPT: {e}")
        return BargeState.ACCEPT
