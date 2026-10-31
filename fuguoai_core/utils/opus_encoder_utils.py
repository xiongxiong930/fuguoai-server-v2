"""PCM 到 Opus 的分帧编码器（复果自有实现）。

面向 TTS「边合成边下发」的场景：调用方把陆续到达的 PCM 字节流交进来，
本类按固定帧长切分、逐帧编码，并**立刻**通过回调把成品帧交还，
因此下游无需等到整段音频合成完毕即可开始推送。

两个必须显式理解的行为点：

1. 不足一帧的尾部样本不会丢弃，而是留在实例内的缓冲区里，等下一次调用
   凑满整帧再编码；只有当调用方声明 `end_of_stream` 时才把残余样本补零
   成最后一帧收尾。**忘记传 end_of_stream 会静默丢掉最后一段尾音。**
2. Opus 编码器内部持有跨帧的预测状态，一个实例只能服务一路音频。
   多路并发（如聊天 TTS 与定时提醒同时发声）必须各自 new 一个实例，
   共用实例会让两路音频互相串扰、听感上出现咔哒声。
"""

import logging
from typing import Any, Callable, Optional

import numpy as np
from opuslib_next import Encoder, constants

logger = logging.getLogger(__name__)

# 语音场景下的编码参数：24kbps 窄带语音足够，复杂度拉满以换取音质
_ENCODER_BITRATE_BPS = 24000
_ENCODER_COMPLEXITY = 10

# 16 位有符号 PCM 的取值边界
_PCM_CEILING = 32767
_PCM_FLOOR = -32768


class OpusEncoderUtils:
    """把连续 PCM 流按帧切成 Opus 包，逐帧交给回调。"""

    def __init__(self, sample_rate: int, channels: int, frame_size_ms: int):
        """Args:
            sample_rate: 输入 PCM 采样率（Hz）
            channels: 声道数（1 单声道 / 2 立体声）
            frame_size_ms: 单个 Opus 帧覆盖的时长（毫秒）
        """
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_size_ms = frame_size_ms

        # 单帧「每声道」样本数，喂给 opus 的就是这个值
        self.frame_size = (sample_rate * frame_size_ms) // 1000
        # 单帧在交错排布下的总样本数（= 每声道样本数 × 声道数）
        self.total_frame_size = self.frame_size * channels

        self.bitrate = _ENCODER_BITRATE_BPS
        self.complexity = _ENCODER_COMPLEXITY

        # 上一次调用剩下的、还不够凑一帧的样本
        self.buffer = np.array([], dtype=np.int16)

        self.encoder = None
        try:
            self.encoder = self._spawn_encoder()
        except Exception as exc:
            logger.error(
                "创建 Opus 编码器失败（采样率=%s 声道=%s）：%s",
                sample_rate,
                channels,
                exc,
            )
            raise RuntimeError("初始化失败") from exc

    def _spawn_encoder(self) -> Encoder:
        """构造并按语音场景调好一个 opuslib 编码器实例。"""
        encoder = Encoder(self.sample_rate, self.channels, constants.APPLICATION_AUDIO)
        encoder.bitrate = self.bitrate
        encoder.complexity = self.complexity
        encoder.signal = constants.SIGNAL_VOICE
        return encoder

    def reset_state(self):
        """丢弃编码器内部预测状态与残留样本，使后续帧从干净状态起算。"""
        self.encoder.reset_state()
        self.buffer = np.array([], dtype=np.int16)

    def encode_pcm_to_opus_stream(
        self,
        pcm_data: bytes,
        end_of_stream: bool,
        callback: Callable[[Any], Any],
    ) -> None:
        """把一段 PCM 追加进流水线，凑满的整帧立刻经 callback 吐出。

        Args:
            pcm_data: 小端 16 位有符号 PCM 字节流。
            end_of_stream: 本次是否为整条流的最后一段；为真时把残余样本
                补零成最后一帧收尾，并清空缓冲区。
            callback: 每编出一帧调用一次，入参为 opus 字节串。
        """
        arrived = self._decode_samples(pcm_data)
        self._warn_if_out_of_range(arrived)
        self.buffer = np.append(self.buffer, arrived)

        whole_frames, leftover = divmod(self.buffer.size, self.total_frame_size)
        consumed = whole_frames * self.total_frame_size
        for index in range(whole_frames):
            cut = index * self.total_frame_size
            self._push(self.buffer[cut : cut + self.total_frame_size], callback)
        self.buffer = self.buffer[consumed:]

        if end_of_stream and self.buffer.size > 0:
            self._push(self._padded_to_full_frame(self.buffer), callback)
            self.buffer = np.array([], dtype=np.int16)

    def _padded_to_full_frame(self, samples: np.ndarray) -> np.ndarray:
        """把尾部残帧右侧补零到整帧长度。"""
        missing = self.total_frame_size - samples.size
        return np.concatenate([samples, np.zeros(missing, dtype=np.int16)])

    def _push(self, frame: np.ndarray, callback: Callable[[Any], Any]) -> None:
        """编码一帧；产出非空才回调。"""
        packet = self._encode_frame(frame)
        if packet:
            callback(packet)

    def _encode_frame(self, frame: np.ndarray) -> Optional[bytes]:
        """调 opuslib 编码单帧。编码器已释放或 opuslib 报错时返回 None。"""
        encoder = getattr(self, "encoder", None)
        if encoder is None:
            return None
        try:
            return encoder.encode(frame.tobytes(), self.frame_size)
        except Exception as exc:
            logger.error("Opus 帧编码失败：%s", exc, exc_info=True)
            return None

    def _decode_samples(self, raw: bytes) -> np.ndarray:
        """按小端 16 位有符号整数解析 PCM 字节流。"""
        return np.frombuffer(raw, dtype="<i2")

    def _warn_if_out_of_range(self, samples: np.ndarray) -> None:
        """样本越出 int16 量程时打点告警（正常解析下不会触发，属兜底）。"""
        if samples.size == 0:
            return
        offscale = (samples > _PCM_CEILING) | (samples < _PCM_FLOOR)
        if np.any(offscale):
            logger.warning(
                "发现越界 PCM 样本 %d 个，前几个：%s",
                int(np.count_nonzero(offscale)),
                samples[offscale][:5],
            )

    def close(self):
        """释放编码器。可重复调用，第二次起为空操作。"""
        if getattr(self, "encoder", None) is None:
            return
        try:
            self.encoder = None
        except Exception as exc:
            logger.error("释放 Opus 编码器失败：%s", exc)
