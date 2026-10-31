"""留言播放：服务端虚拟对端（docs/VOICEMAIL_DESIGN.md §5，固件零改动）。

原理：设备收 SetIntercom start 进对讲态后，协议层剥 [4B seq] 解 opus 播放。
本模块冒充对端 "vm-server"：按 20ms 实钟节奏逐帧下发 [4B BE seq][opus]，
播完 SetIntercom stop。期间设备 mic 上行帧由 intercom_registry._vm_sinks
静默丢弃（不注册会话、不占忙线表）。

节奏铁律（60ms 实验教训：帧节奏是固件命门）：
- 用 loop.time() 绝对时刻表调度（target = t0 + i*0.02），不用 sleep 累积（防漂移）。
- 绝不突发灌帧。

抢占规则（R2）：播放中出现真实呼叫/低层会话 → 立即停止，留言保持 new 重投。
"""

import asyncio
import logging
import struct
import time

from fuguoai_core.utils.voicemail_store import get_voicemail_store, read_fgvm, SAMPLE_RATE
from fuguoai_core.utils.intercom_registry import get_intercom_registry, _get_conn

TAG = __name__
_logger = logging.getLogger(TAG)

VM_PEER = "vm-server"       # 虚拟对端标识（下发给设备的 peer 字段）
FRAME_SEC = 0.02            # 20ms/帧
PRE_ROLL_SEC = 0.3          # start 后等设备进对讲态
DRAIN_SEC = 0.5             # 尾帧后排空再 stop
ABORT_CHECK_EVERY = 25      # 每 25 帧（0.5s）查一次抢占
WORKER_INTERVAL = 10.0      # 投递轮询间隔


def _set_intercom_cmd(peer: str, action: str) -> dict:
    """与 intercom_call_store._set_intercom_cmd 完全同构。"""
    return {
        "type": "iot",
        "commands": [{
            "name": "DeviceControl",
            "method": "SetIntercom",
            "parameters": {"peer": peer, "action": action},
        }],
    }


def _device_idle(device_id: str) -> bool:
    """投递前置：在线 + 不在低层透传会话 + 不在活跃呼叫单。"""
    try:
        from fuguoai_core.utils.connection_registry import get_connection_registry
        from fuguoai_core.utils.intercom_registry import get_intercom_registry
        from fuguoai_core.utils.intercom_call_store import get_call_orchestrator
        if not get_connection_registry().is_online(device_id):
            return False
        if get_intercom_registry().peer_of(device_id):
            return False
        orch = get_call_orchestrator()
        with orch._lock:
            if device_id in orch._by_device:
                return False
        return True
    except Exception:
        return False


def _device_supports_playmessage(conn) -> bool:
    """设备是否已上报 DeviceControl.PlayMessage 方法（能力探测双路用）。

    物模型挂在 conn.iot_descriptors（key 为设备名，value 为 IotDescriptor）。
    """
    descs = getattr(conn, "iot_descriptors", None)
    if not descs:
        return False
    for name, desc in descs.items():
        if name.lower() != "devicecontrol":
            continue
        methods = getattr(desc, "methods", None) or []
        for m in methods:
            mname = m.get("name") if isinstance(m, dict) else getattr(m, "name", None)
            if mname and mname.lower() == "playmessage":
                return True
    return False


class VoicemailPlayer:
    def __init__(self):
        self._playing = set()       # 播放中的 device_id（防重入）
        self._worker_task = None

    # ────────────────────────── 单条播放 ──────────────────────────

    async def play_one(self, device_id: str, msg: dict) -> bool:
        """向设备播一条留言（能力探测双路，2026-08-13）。

        - 设备已上报 DeviceControl.PlayMessage → 走对话音频通道推原声（v2.0.0-line 等新
          固件，本机无自由文本 TTS；原声经转码后走 SetOnServerAudio，设备念的是录音原声，
          不做 ASR、不依赖固件 TTS，零识别误差）；
        - 否则回退 vm-server / SetIntercom 虚拟对端通道（v1.6.0 等老固件）。
        """
        conn = _get_conn(device_id)
        if conn is not None and _device_supports_playmessage(conn):
            return await self.play_one_audio(device_id, msg, conn)
        return await self.play_one_vm(device_id, msg)

    async def play_one_vm(self, device_id: str, msg: dict) -> bool:
        """vm-server / SetIntercom 虚拟对端投递（v1.6.0 等老固件）。"""
        from fuguoai_core.utils.connection_registry import get_connection_registry
        from fuguoai_core.utils.intercom_registry import get_intercom_registry, _get_conn

        store = get_voicemail_store()
        msg_id = msg["msg_id"]
        try:
            _, frames = read_fgvm(store.audio_path(device_id, msg_id))
        except Exception as e:
            _logger.warning(f"[voicemail] fgvm 读取失败 {msg_id}: {e}，标记 deleted")
            store.set_status(device_id, msg_id, "deleted")
            return False
        if not frames:
            store.set_status(device_id, msg_id, "deleted")
            return False

        reg = get_connection_registry()
        intercom = get_intercom_registry()
        intercom.vm_sink_add(device_id)
        started = False
        try:
            if not await reg.send_to_device(device_id, _set_intercom_cmd(VM_PEER, "start")):
                return False
            started = True
            await asyncio.sleep(PRE_ROLL_SEC)

            loop = asyncio.get_running_loop()
            t0 = loop.time()
            seq = 0
            for i, opus in enumerate(frames):
                # 实钟节奏：绝对时刻表，不累积漂移
                target = t0 + i * FRAME_SEC
                delay = target - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                if i % ABORT_CHECK_EVERY == 0:
                    if not self._still_ok(device_id):
                        _logger.info(f"[voicemail] 播放被抢占/设备离线，中止 {msg_id} @frame{i}")
                        return False
                conn = _get_conn(device_id)
                if conn is None:
                    _logger.info(f"[voicemail] 设备连接消失，中止 {msg_id} @frame{i}")
                    return False
                frame = struct.pack(">I", seq) + opus
                seq = (seq + 1) & 0xFFFFFFFF
                try:
                    await conn.safe_send_bytes(frame)
                except Exception:
                    return False
            await asyncio.sleep(DRAIN_SEC)
            store.set_status(device_id, msg_id, "played")
            _logger.info(f"[voicemail] 播放完成 {msg_id} -> {device_id} frames={len(frames)}")
            return True
        finally:
            if started:
                try:
                    await reg.send_to_device(device_id, _set_intercom_cmd(VM_PEER, "stop"))
                except Exception:
                    pass
            intercom.vm_sink_remove(device_id)

    async def play_one_audio(self, device_id: str, msg: dict, conn) -> bool:
        """v2.0.0-line 等新固件：原音频经对话音频通道推送（零 TTS、原声无损）。

        流程：fgvm 原 16k/20ms opus → 16k PCM → 重采样 conn.sample_rate → 60ms opus
        → 对话音频通道(start/FIRST/LAST/stop)，与聊天回复同源(SetOnServerAudio)。
        设备念的是录音原声，不做 ASR、不依赖固件 TTS（之前的 ASR→PlayMessage 文本播报
        会因识别误差丢失原声，现已废弃）。复用了提醒 _speak_via_chat 的同款通道。
        """
        import opuslib_next

        store = get_voicemail_store()
        msg_id = msg["msg_id"]
        try:
            _, frames = read_fgvm(store.audio_path(device_id, msg_id))
        except Exception as e:
            _logger.warning(f"[voicemail] fgvm 读取失败 {msg_id}: {e}，标记 deleted")
            store.set_status(device_id, msg_id, "deleted")
            return False
        if not frames:
            store.set_status(device_id, msg_id, "deleted")
            return False

        # 原 16k/20ms opus → 16k/mono/s16le PCM
        try:
            dec = opuslib_next.Decoder(SAMPLE_RATE, 1)
            pcm = bytearray()
            for fr in frames:
                try:
                    pcm += dec.decode(fr, 320)
                except Exception:
                    pcm += b"\x00" * 640
        except Exception as e:
            _logger.warning(f"[voicemail] opus 解码失败 {msg_id}: {e}")
            return False

        # 重采样到对话通道采样率（通常为 24k），再编码 60ms opus
        sr = getattr(conn, "sample_rate", 24000) or 24000
        try:
            pcm_chat = _resample_pcm(bytes(pcm), SAMPLE_RATE, sr)
            opus = _pcm_to_chat_opus(pcm_chat, sr)
        except Exception as e:
            _logger.warning(f"[voicemail] 对话通道转码失败 {msg_id}: {e}")
            return False
        if not opus:
            _logger.warning(f"[voicemail] 转码为空，留待重试 {msg_id}")
            return False

        # 经对话音频通道推送（与聊天同源；设备原声播报）
        ok = await _speak_opus_via_chat(conn, device_id, opus, f"voicemail {msg_id}")
        if ok:
            store.set_status(device_id, msg_id, "played")
        return ok

    def _still_ok(self, device_id: str) -> bool:
        """播放途中检查：真实呼叫/低层会话出现 或 设备离线 → 中止（真呼叫优先）。"""
        try:
            from fuguoai_core.utils.connection_registry import get_connection_registry
            from fuguoai_core.utils.intercom_registry import get_intercom_registry
            from fuguoai_core.utils.intercom_call_store import get_call_orchestrator
            if not get_connection_registry().is_online(device_id):
                return False
            if get_intercom_registry().peer_of(device_id):
                return False
            orch = get_call_orchestrator()
            with orch._lock:
                if device_id in orch._by_device:
                    return False
            return True
        except Exception:
            return False

    # ────────────────────────── 投递编排 ──────────────────────────

    async def deliver_for_device(self, device_id: str):
        """顺序播完某设备的全部待播留言（先留先播；单设备防重入）。"""
        if device_id in self._playing:
            return
        self._playing.add(device_id)
        try:
            store = get_voicemail_store()
            for msg in store.pending(device_id):
                if not _device_idle(device_id):
                    return  # 设备忙/离线，本轮放弃，下轮 worker 重试
                ok = await self.play_one(device_id, msg)
                if not ok:
                    return  # 被抢占/失败：剩余留言留待下轮
                await asyncio.sleep(1.0)  # 两条留言间隔 1s
        finally:
            self._playing.discard(device_id)

    async def _worker(self):
        """每 WORKER_INTERVAL 秒扫一遍有待播留言的设备，逐台投递。"""
        store = get_voicemail_store()
        while True:
            try:
                await asyncio.sleep(WORKER_INTERVAL)
                for device_id in store.devices_with_pending():
                    if _device_idle(device_id):
                        await self.deliver_for_device(device_id)
            except asyncio.CancelledError:
                return
            except Exception as e:
                _logger.warning(f"[voicemail] worker 异常(继续): {e}")

    def ensure_worker(self):
        if self._worker_task is None or self._worker_task.done():
            try:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = asyncio.get_event_loop()
                self._worker_task = loop.create_task(self._worker())
            except Exception:
                self._worker_task = None


def _resample_pcm(pcm: bytes, from_sr: int, to_sr: int) -> bytes:
    """16k/mono/s16le → to_sr/mono/s16le。

    使用 scipy.signal.resample_poly（多相 FIR，抗镜像/抗混叠）替代原 np.interp
    线性插值——后者在 16k→24k 升采样时会注入 ~-20dB 高频镜像噪声（听感为嘶嘶/
    风声底噪，实测比正确重采样多 ~39dB）。该噪声在 voice 402001 等标准/精品音色
    （腾讯返回 16k，服务端升采样到 conn.sample_rate=24k）上每次 TTS 都持续存在。
    scipy 不可用时的回退仍是线性插值（旧行为，不引入新故障）。
    """
    if from_sr == to_sr or not pcm:
        return pcm
    try:
        import numpy as np
        from fractions import Fraction
        from scipy.signal import resample_poly
        arr = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        if arr.size == 0:
            return b""
        frac = Fraction(to_sr, from_sr).limit_denominator(1000)
        up, down = frac.numerator, frac.denominator
        res = resample_poly(arr, up, down)
        res = np.clip(res, -32768.0, 32767.0).astype("<i2")
        return res.tobytes()
    except Exception as e:
        _logger.warning(f"[voicemail] scipy 重采样不可用，回退线性插值: {e}")
        try:
            import numpy as np
            arr = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
            n = len(arr)
            if n == 0:
                return b""
            new_n = int(round(n * to_sr / from_sr))
            new_idx = np.linspace(0, n - 1, new_n)
            res = np.interp(new_idx, np.arange(n), arr)  # 端点外推自动钳制
            return res.astype("<i2").tobytes()
        except Exception as e2:
            _logger.warning(f"[voicemail] numpy 重采样失败，回退纯 Python: {e2}")
            import array
            samples = array.array("h")
            samples.frombytes(pcm)
            n = len(samples)
            if n == 0:
                return b""
            new_n = int(round(n * to_sr / from_sr))
            out = array.array("h")
            for i in range(new_n):
                pos = i * (n - 1) / max(1, new_n - 1)
                lo = int(pos)
                hi = min(lo + 1, n - 1)
                frac = pos - lo
                out.append(int(round(samples[lo] * (1 - frac) + samples[hi] * frac)))
            return out.tobytes()


def _pcm_to_chat_opus(pcm: bytes, sample_rate: int) -> list:
    """PCM → 对话通道 opus（sample_rate / 60ms），与正常聊天 TTS 字节格式一致。

    用独立 OpusEncoderUtils（不碰 conn.opus_encoder 共享状态，避免与进行中的聊天串帧）。
    """
    from fuguoai_core.utils.opus_encoder_utils import OpusEncoderUtils
    enc = OpusEncoderUtils(sample_rate=sample_rate, channels=1, frame_size_ms=60)
    frames = []
    enc.encode_pcm_to_opus_stream(pcm, True, lambda d: frames.append(d))
    return frames


async def _speak_opus_via_chat(conn, device_id: str, opus_frames: list, label: str) -> bool:
    """把现成 opus 帧列表经对话音频通道(start/FIRST/LAST/stop)推到设备。

    与提醒 _speak_via_chat 同款通道；文本传空(无字幕)。帧由 AudioRateController 按 60ms
    实时节奏逐帧下发，长留言不会一次性灌爆设备缓冲。
    """
    from fuguoai_core.handle.sendAudioHandle import sendAudioMessage, send_tts_message
    from fuguoai_core.providers.tts.dto.dto import SentenceType

    try:
        await send_tts_message(conn, "start")
        await sendAudioMessage(conn, SentenceType.FIRST, opus_frames, "")
        await sendAudioMessage(conn, SentenceType.LAST, [], None)
        await send_tts_message(conn, "stop", None)
        _logger.info(f"[voicemail] 原声已通过对话通道下发 {label} "
                     f"sr={getattr(conn, 'sample_rate', 24000)} frames={len(opus_frames)}")
        return True
    except Exception as e:
        _logger.warning(f"[voicemail] 对话通道推送失败 {label} did={device_id}: {e}")
        return False


_PLAYER = None


def get_voicemail_player() -> VoicemailPlayer:
    global _PLAYER
    if _PLAYER is None:
        _PLAYER = VoicemailPlayer()
    return _PLAYER
