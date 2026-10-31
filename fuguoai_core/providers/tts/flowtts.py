"""
复果 TTS provider —— 腾讯云对话式 TTS（FlowTTS，双向 WebSocket 流式协议）

产品：腾讯云 TRTC「语音合成」(FlowTTS)，对话场景主推模型 flow_02_turbo。
特性：超低延迟（首包 ~300ms）、拟人度高、支持声音克隆音色（VoiceId）。

接入方式：WebSocket 双向流式。鉴权走 URL 查询参数（HMAC-SHA1 + Base64），
与腾讯云「流式文本语音合成」(tts.cloud.tencent.com/stream_wsv2) 同源签名算法，
但 host/path 与业务消息格式不同（协议见腾讯云官方文档 / FlowTTS 仓库
docs/ws_bidirection_protocol.md）。

启用（默认关闭，候选 provider，不替换生产主路径）：
- 在 TRTC 控制台建应用拿 SdkAppId；
- voice 填克隆出的 VoiceId（由 voiceprint.clone_enabled 一次录音两用生成），或直接填精品音色 ID；
- selected_module.TTS 改为 FlowTTS。
未填 SdkAppId / 不改 selected_module.TTS 时本模块不被实例化，不影响 EdgeTTS/TencentTTS。

实现要点：
- text_to_speak：返回 mp3 字节（与 EdgeTTS 一致，下游统一 opus 编码下发设备）；
- text_to_stream：raw PCM(16bit/单声道/sample_rate) 逐片回调 pcm_callback，供 base.py 即时 opus 下发；
- 长文本按 <=1000 字符分片发 ContinueSession；SentenceAudio.Data.Audio 为 Base64，按到达顺序拼接；
- 全程 websockets 异步驱动，不阻塞事件循环。

注意：本 provider 仅完成协议接入与离线 mock 验证；真实音色/延迟/听感需在你腾讯云
账号 + 真机（提供 SdkAppId、翻 selected_module.TTS）下验证，未经线上实测不得默认开启。
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import uuid

import websockets
from fuguoai_config.logger import setup_logging
from fuguoai_core.providers.tts.base import TTSProviderBase

TAG = __name__
logger = setup_logging()

# FlowTTS 接入点（官方协议文档）
_FLOW_HOST = "flowtts.cloud.tencent.com"
_FLOW_PATH = "/api/v1/flow_tts/bidirection"

# ContinueSession 单次文本上限（官方约束）
_MAX_TEXT_CHUNK = 1000


class TTSProvider(TTSProviderBase):
    TTS_PARAM_CONFIG = [
        ("ttsVolume", "volume", 0, 10, 1, float),
        ("ttsRate", "speed", 0.5, 2.0, 1.0, float),
        ("ttsPitch", "pitch", -12, 12, 0, float),
    ]

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.secret_id = config.get("secret_id")
        self.secret_key = config.get("secret_key")
        self.sdk_app_id = config.get("sdk_app_id")
        self.voice = config.get("voice")  # 克隆 VoiceId 或精品音色 ID（必填）
        self.region = config.get("region", "ap-guangzhou")
        self.model = config.get("model", "flow_02_turbo")
        self.language = config.get("language", "zh")
        self.sample_rate = int(config.get("sample_rate", 24000))
        self.speed = float(config.get("speed", 1.0))
        self.volume = float(config.get("volume", 1.0))
        self.pitch = float(config.get("pitch", 0.0))
        self.audio_file_type = config.get("format", "mp3")
        self.connection_id = str(uuid.uuid4())
        self._validate()

    def _validate(self):
        missing = [k for k in ("secret_id", "secret_key", "sdk_app_id", "voice")
                   if not getattr(self, k, None)]
        if missing:
            raise ValueError(
                f"FlowTTS 配置缺失: {missing}（需在 TTS.FlowTTS 填 secret_id/secret_key/"
                f"sdk_app_id 与 voice；并置 selected_module.TTS=FlowTTS）"
            )

    # ── 鉴权 URL 构造（HMAC-SHA1，与腾讯云 stream_wsv2 同源算法）──
    def _build_url(self):
        timestamp = int(time.time())
        expired = timestamp + 86400
        params = {
            "Action": "TextToSpeechBidirection",
            "SecretId": self.secret_id,
            "SdkAppId": self.sdk_app_id,
            "Timestamp": timestamp,
            "Expired": expired,
            "ConnectionId": self.connection_id,
        }
        # 签名原文：GET + path + "?" + 排序(排除 Signature)的 k=v（原始值，不 urlencode）
        sorted_params = sorted(params.items())
        sign_str = (
            f"GET{_FLOW_PATH}?"
            + "&".join(f"{k}={v}" for k, v in sorted_params)
        )
        signature = base64.b64encode(
            hmac.new(self.secret_key.encode("utf-8"), sign_str.encode("utf-8"),
                     hashlib.sha1).digest()
        ).decode("ascii")
        params["Signature"] = signature
        query = "&".join(
            f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in sorted(params.items())
        )
        return f"wss://{_FLOW_HOST}{_FLOW_PATH}?{query}"

    @staticmethod
    def _msg(event, connection_id, session_id, data):
        return json.dumps({
            "Event": event,
            "ConnectionId": connection_id,
            "SessionId": session_id,
            "MessageId": str(uuid.uuid4()),
            "Data": data,
        }, ensure_ascii=False)

    def _chunk_text(self, text):
        text = text or ""
        if len(text) <= _MAX_TEXT_CHUNK:
            return [text] if text else []
        return [text[i:i + _MAX_TEXT_CHUNK] for i in range(0, len(text), _MAX_TEXT_CHUNK)]

    async def _synthesize(self, text, fmt, pcm_callback=None):
        """核心：建立 WS、握手、发文本、收音频。

        - fmt="pcm"：每片 SentenceAudio 解码后回调 pcm_callback（流式路径）；
        - fmt="mp3"/"wav"：拼装为完整字节返回（非流式 text_to_speak 路径）。
        返回拼接后的音频字节（两种路径均返回，供 text_to_speak 复用）。
        """
        url = self._build_url()
        collected = bytearray()
        session_id = ""

        async with websockets.connect(
            url, open_timeout=10, close_timeout=5, ping_interval=None, max_size=None
        ) as ws:
            # 1) StartSession（SessionId 留空，等服务端分配）
            await ws.send(self._msg("StartSession", self.connection_id, "",
                                    {
                                        "Language": self.language,
                                        "Model": self.model,
                                        "AudioFormat": {
                                            "Format": fmt,
                                            "SampleRate": self.sample_rate,
                                            "BitRate": 128,
                                        },
                                        "Voice": {
                                            "VoiceId": self.voice,
                                            "Speed": self.speed,
                                            "Volume": self.volume,
                                            "Pitch": self.pitch,
                                        },
                                    }))
            # 2) 收 SessionStart（带 SessionId）
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=15)
                msg = json.loads(raw) if isinstance(raw, str) else None
                if msg and msg.get("Event") == "SessionStart":
                    session_id = msg.get("SessionId", "")
                    break
                if msg and msg.get("Event") in ("SessionError",):
                    raise RuntimeError(f"FlowTTS 会话启动失败: {msg}")
                # 其余事件（心跳等）忽略

            # 3) 分片发 ContinueSession（<=1000 字符/片）
            for piece in self._chunk_text(text):
                await ws.send(self._msg("ContinueSession", self.connection_id,
                                        session_id, {"Text": piece}))
            # 4) FinishSession
            await ws.send(self._msg("FinishSession", self.connection_id,
                                    session_id, {}))

            # 5) 收音频直到 SessionEnd
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=20)
                except asyncio.TimeoutError:
                    raise RuntimeError("FlowTTS 接收音频超时")
                if isinstance(raw, bytes):
                    # 协议音频走 Data.Audio(JSON)，二进制帧通常为空/保活，忽略
                    continue
                msg = json.loads(raw)
                event = msg.get("Event")
                if event == "SessionEnd":
                    break
                if event in ("SessionError", "SentenceError"):
                    raise RuntimeError(f"FlowTTS 合成错误: {msg}")
                if event == "SentenceAudio":
                    audio_b64 = (msg.get("Data") or {}).get("Audio") or ""
                    if audio_b64:
                        chunk = base64.b64decode(audio_b64)
                        collected.extend(chunk)
                        if pcm_callback is not None:
                            pcm_callback(chunk)
        return bytes(collected)

    async def text_to_speak(self, text, output_file):
        # 非流式：返回 mp3 字节（与 EdgeTTS 一致，下游统一 opus 编码下发设备）
        audio = await self._synthesize(text, self.audio_file_type, pcm_callback=None)
        if output_file:
            os.makedirs(os.path.dirname(output_file) or ".", exist_ok=True)
            with open(output_file, "wb") as f:
                f.write(audio)
            return None
        return audio

    async def text_to_stream(self, text, pcm_callback):
        # 流式：raw PCM 逐片回调
        await self._synthesize(text, "pcm", pcm_callback=pcm_callback)
        return None
