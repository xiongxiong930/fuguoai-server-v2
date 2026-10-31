import time
import base64
import audioop
from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler

from fuguoai_core.handle.textMessageHandler import TextMessageHandler
from fuguoai_core.handle.textMessageType import TextMessageType

TAG = __name__


class AudioPlaybackRefTextMessageHandler(TextMessageHandler):
    """处理固件上行的回声参考流 audio_playback_ref（全双工后端 AEC 用）

    契约见 Claw/fuguoai-ble-firmware/src/BACKEND_AEC_COORDINATION.md：
    固件在 TTS 播放期间把"实际播放的 TTS PCM"独立 opus 编码后，经文本通道发来：
        {"type": "audio_playback_ref", "data": "<base64 opus>", "ts": <playout_ms>, "rate": <int>}
    后端解码并重采样到 16k（与麦上行一致），以服务端到达时刻存入 aec_audio_cache，
    供 _apply_aec 作为远端参考消回声。后端未升级时该消息被忽略（向后兼容）。
    """

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.AUDIO_PLAYBACK_REF

    async def handle(self, conn: "ConnectionHandler", msg_json: Dict[str, Any]) -> None:
        # 🔴 2026-08-03 FIX：不再要求 hello 里携带 features.aec。
        # 固件发 audio_playback_ref 本身就是"我能给参考"的充分证明——收到第一条参考帧
        # 即自动开启 client_aec，_apply_aec 和 sendAudioHandle 的自喂参考路径同时激活。
        # 旧行为：client_aec=False → return → 参考帧全丢弃 → AEC 无数据 → 回声直通 ASR
        # → "我没听清"自激死循环。修复：由参考流的到达驱动开启，而非依赖 hello 协商。
        if not getattr(conn, "client_aec", False):
            conn.logger.bind(tag=TAG).info(
                "[AEC] 收到第一条 audio_playback_ref，自动开启服务端 AEC"
            )
            conn.client_aec = True
            # 确保 aec_audio_cache 已初始化（sendAudioHandle 的自喂参考也用同一缓存）
            if not hasattr(conn, "aec_audio_cache"):
                conn.aec_audio_cache = {}
                conn.aec_audio_cache_time = {}

        data_b64 = msg_json.get("data")
        if not data_b64:
            return
        try:
            opus_bytes = base64.b64decode(data_b64)
        except Exception as e:
            conn.logger.bind(tag=TAG).debug(f"[AEC] 参考 base64 解码失败: {e}")
            return
        if not opus_bytes:
            return

        rate = int(msg_json.get("rate", 16000))

        # 按固件编码采样率建解码器（速率变化则重建）
        try:
            if not hasattr(conn, "_ref_opus_decoder") or getattr(conn, "_ref_rate", 0) != rate:
                import opuslib_next
                conn._ref_opus_decoder = opuslib_next.Decoder(rate, 1)
                conn._ref_rate = rate
            pcm = conn._ref_opus_decoder.decode(bytes(opus_bytes), 960)
        except Exception as e:
            conn.logger.bind(tag=TAG).debug(f"[AEC] 参考 opus 解码失败: {e}")
            return
        if not pcm:
            return

        # 重采样到与麦一致的 16k（麦克风上行约定 16k 单声道）
        if rate != 16000:
            try:
                pcm, _ = audioop.ratecv(pcm, 2, 1, rate, 16000, None)
            except Exception as e:
                conn.logger.bind(tag=TAG).debug(f"[AEC] 参考重采样失败({rate}->{16000}): {e}")
                return

        # 参考缓存时钟：统一用服务端到达时刻 int(time()*1000)。
        # 曾试固件同源 ts(Path C 配套)：实测固件 audio_playback_ref.ts 与麦头 ts 时基不一致
        # (Δt_avg=-2273 天)，对齐错乱导致播放期残差飙升、回声漏进 ASR，已回退到服务端时钟
        # (与麦同源，经验证对齐稳定、AEC 正常生效)。
        ts = int(time.time() * 1000)
        if not hasattr(conn, "aec_audio_cache"):
            conn.aec_audio_cache = {}
            conn.aec_audio_cache_time = {}
        conn.aec_audio_cache[ts] = bytes(pcm)
        conn.aec_audio_cache_time[ts] = time.time()
        conn._aec_firmware_ref = True
        # 🔴 2026-08-03 FIX(#254)：记录最近一次收到固件播放参考的服务器时间。
        # 固件在"把音频写入 I2S/DMA"时就发 ref，而实际出声晚 5~9s(DMA 缓冲深)。
        # 故 ref 持续到达 = 设备仍在播；barge-in 用此延长"播放中"判定，避免后端队列
        # 清空后设备还在响的错位窗口里，残留回声被当非播放期真人插话误切断。
        conn._last_playback_ref_at = time.time()
