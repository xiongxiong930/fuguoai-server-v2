"""CosyVoice 远程 TTS provider（工作室本机部署）。

服务器端只负责把文本 POST 到工作室机暴露的 HTTP 接口，拿回 16k/mono/s16le PCM，
按设备采样率重采样后交给 opus 编码器下发；工作室机本身由用户侧部署
（cosyvoice_server.py + voices/），不在本仓职责内。

失败语义：网络/超时/HTTP 非 2xx 一律抛异常，交由上层（路由器的 base 兜底机制）
降级到腾讯云，绝不静默返回空音频（避免设备静音）。
"""
import asyncio
import logging

import requests

from fuguoai_core.providers.tts.base import TTSProviderBase

logger = logging.getLogger(__name__)

# 工作室机合成超时（秒）；连不上/超时即抛异常 → 上层降级腾讯云
_DEFAULT_TIMEOUT = 8.0


class TTSProvider(TTSProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.base_url = (config.get("base_url") or "").rstrip("/")
        if not self.base_url:
            raise ValueError("CosyVoice provider 必须配置 base_url(工作室机地址)")
        self.timeout = float(config.get("timeout_ms", 8000)) / 1000.0
        self.endpoint = (config.get("endpoint") or "/tts/stream").rstrip("/")
        # 方言 → 说话人(spk)；命中则随请求下发，未命中由工作室机用默认说话人
        self.dialect_speakers = (
            config.get("dialect_speakers") or config.get("speakers") or {}
        )
        # CosyVoice 返回原始 PCM；一次性路径经 decode_to_pcm("pcm") 直通（认定 16k/mono/s16le）
        self.audio_file_type = config.get("format", "pcm")
        self.src_sample_rate = int(config.get("sample_rate", 16000))

    # ------------------------------------------------------------------
    # 说话人选择：优先按 conn 上解析出的 dialect 选 spk，否则用首个配置说话人
    # ------------------------------------------------------------------
    def _speaker_for(self, dialect=None):
        if dialect and dialect in self.dialect_speakers:
            return self.dialect_speakers[dialect]
        if isinstance(self.dialect_speakers, dict) and self.dialect_speakers:
            return next(iter(self.dialect_speakers.values()))
        return None

    def _target_sample_rate(self):
        return (
            self.conn.sample_rate
            if self.conn and getattr(self.conn, "sample_rate", None)
            else self.src_sample_rate
        )

    def _resample(self, pcm: bytes) -> bytes:
        """16k PCM → 设备率 PCM（仅率不一致时重采样，避免变调）。"""
        target = self._target_sample_rate()
        if not pcm or target == self.src_sample_rate:
            return pcm
        try:
            from fuguoai_core.utils.voicemail_player import _resample_pcm

            return _resample_pcm(pcm, self.src_sample_rate, target)
        except Exception as e:
            logger.warning(f"CosyVoice PCM 重采样失败, 原样下发: {e}")
            return pcm

    def _post(self, text):
        """一次性 POST，返回 16k/mono/s16le PCM 原始字节。失败抛异常。"""
        url = self.base_url + self.endpoint
        speaker = self._speaker_for(getattr(self, "_dialect", None))
        payload = {
            "text": text,
            "audio_format": "pcm",
            "sample_rate": self.src_sample_rate,
        }
        if speaker:
            payload["speaker"] = speaker
        try:
            resp = requests.post(url, json=payload, timeout=self.timeout)
        except requests.RequestException as e:
            raise RuntimeError(f"CosyVoice 连接失败({url}): {e}")
        if resp.status_code != 200:
            raise RuntimeError(f"CosyVoice 返回 {resp.status_code}: {resp.text[:200]}")
        return resp.content

    async def text_to_speak(self, text, output_file):
        """一次性合成：返回原始 16k PCM 字节（或写入文件）。

        注意：返回 RAW 16k PCM（audio_file_type=pcm），由下游 decode_to_pcm("pcm")
        直通 + 重采样到设备率，避免与提醒路径的 16k 约定冲突。
        """
        if not text or not text.strip():
            return None
        raw = await asyncio.to_thread(self._post, text)
        if output_file:
            with open(output_file, "wb") as f:
                f.write(raw)
            return None
        return raw

    async def text_to_stream(self, text, pcm_callback):
        """流式合成：拉回 16k PCM → 按设备率重采样 → 逐段回调（与 base 流式路径对接）。"""
        if not text or not text.strip():
            return None
        raw = await asyncio.to_thread(self._post, text)
        pcm = self._resample(raw)
        if pcm:
            pcm_callback(pcm)
        return None
