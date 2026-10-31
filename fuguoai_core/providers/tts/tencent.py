import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timezone
import urllib.parse

import logging

logger = logging.getLogger(__name__)

import requests
import websockets

from fuguoai_core.providers.tts.base import TTSProviderBase

# 腾讯云 TTS 外部 HTTP 调用超时边界（秒）。原实现 requests.post 无 timeout，
# 服务挂起即在事件循环上永久阻塞全进程 WS/对讲/events；统一加边界 + 线程池下沉。
_REQUEST_TIMEOUT = 10


class TTSProvider(TTSProviderBase):
    TTS_PARAM_CONFIG = [
        ("ttsVolume", "volume", -10, 10, 0, lambda v: round(float(v), 1)),
        ("ttsRate", "speed", -2, 6, 0, lambda v: round(float(v), 2)),
    ]

    # 腾讯云标准音色数字 ID；设备私有配置若带入 Edge 风格字符串 voice，回落此值
    DEFAULT_VOICE = 1005  # 智莉，最接近 Edge 晓晓音色

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        self.appid = config.get("appid")
        self.secret_id = config.get("secret_id")
        self.secret_key = config.get("secret_key")
        self.voice = self._resolve_voice(config)
        self.api_url = "https://tts.tencentcloudapi.com"
        self.region = config.get("region")
        self.output_file = config.get("output_dir")
        self.audio_file_type = config.get("format", "wav")

        speed = config.get("speed", "0")
        self.speed = self._norm_speed(speed)
        volume = config.get("volume", "0")
        self.volume = int(volume) if volume else 0
        self._apply_percentage_params(config)

    @staticmethod
    def _norm_speed(v):
        """腾讯 Speed 归一化（官方文档：范围 [-2,6]，可保留 2 位小数，如 0.5/1.25）。

        档位表：-2→0.6x / -1→0.8x / 0→1.0x(默认) / 1→1.2x / 2→1.5x / 6→2.5x。
        角色档案（spec 附录 A）的出厂语速 ±0.5 是合法 Speed 原值，**无需换算**。
        非法/越界回落 0（常速）——绝不把猜测值送进 API。
        实例默认 speed 与 per-call speed（text_to_speak/text_to_stream 形参）共用。
        """
        try:
            f = round(float(v), 2)
        except (TypeError, ValueError):
            return 0
        if f < -2 or f > 6:
            logger.warning("TencentTTS speed=%r 越界[-2,6]，回落 0", v)
            return 0
        return f

    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_voice(config):
        """TencentTTS 的 VoiceType 必须是数字 ID。

        设备私有配置可能带入 Edge 风格字符串 voice（如 'zh-CN-YunxiNeural'），
        非数字时回落默认数字 voice，避免 int() 崩溃使整个 TTS 组件实例化失败。
        """
        raw = config.get("private_voice") or config.get("voice")
        if raw is None or isinstance(raw, bool):
            return TTSProvider.DEFAULT_VOICE
        if isinstance(raw, int):
            return raw
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.warning(
                "TencentTTS voice=%r 非数字，回落默认 %s",
                raw,
                TTSProvider.DEFAULT_VOICE,
            )
            return TTSProvider.DEFAULT_VOICE

    def _resolve_sample_rate(self):
        """超自然大模型/大模型音色(5xxxxx/6xxxxx)支持 24k，标准/精品仅 8k/16k。

        设备采样率常为 24000，超自然音色直接给 24k 避免降采样损失音质；
        其余音色按腾讯云约束钳 16000，否则 InvalidParameterValue 致整段静音。
        """
        sr = self.conn.sample_rate if self.conn else 16000
        if isinstance(self.voice, int) and 500000 <= self.voice < 700000:
            return sr if sr <= 24000 else 24000
        return sr if sr <= 16000 else 16000

    # ------------------------------------------------------------------
    # TC3-HMAC-SHA256 鉴权头构建（腾讯云协议，算法逐字保留）
    # ------------------------------------------------------------------
    def _sign_tencent_request(self, payload):
        """按腾讯云 TC3-HMAC-SHA256 规范生成请求鉴权头。"""
        timestamp = int(time.time())
        utc_date = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime(
            "%Y-%m-%d"
        )
        service = "tts"
        credential_scope = f"{utc_date}/{service}/tc3_request"
        algorithm = "TC3-HMAC-SHA256"

        canonical_headers = (
            f"content-type:application/json\n" f"host:tts.tencentcloudapi.com\n"
        )
        signed_headers = "content-type;host"
        payload_hash = hashlib.sha256(
            json.dumps(payload).encode("utf-8")
        ).hexdigest()
        canonical_request = (
            f"POST\n/\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
        )
        hashed_canonical = hashlib.sha256(
            canonical_request.encode("utf-8")
        ).hexdigest()
        string_to_sign = (
            f"{algorithm}\n"
            f"{timestamp}\n"
            f"{credential_scope}\n"
            f"{hashed_canonical}"
        )
        secret_date = self._hmac_sha256(f"TC3{self.secret_key}".encode(), utc_date)
        secret_service = self._hmac_sha256(secret_date, service)
        secret_signing = self._hmac_sha256(secret_service, "tc3_request")
        signature = hmac.new(
            secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return {
            "Content-Type": "application/json",
            "Host": "tts.tencentcloudapi.com",
            "Authorization": (
                f"{algorithm} "
                f"Credential={self.secret_id}/{credential_scope}, "
                f"SignedHeaders={signed_headers}, "
                f"Signature={signature}"
            ),
            "X-TC-Action": "TextToVoice",
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Version": "2019-08-23",
            "X-TC-Region": self.region,
            "X-TC-Language": "zh-CN",
        }

    @staticmethod
    def _hmac_sha256(key, msg):
        if isinstance(msg, str):
            msg = msg.encode("utf-8")
        return hmac.new(key, msg, hashlib.sha256).digest()

    # ------------------------------------------------------------------
    # 合成入口：外部 HTTP 调用下沉线程池，避免冻结事件循环
    # ------------------------------------------------------------------
    async def text_to_speak(self, text, output_file, speed=None):
        # 请求体字段为腾讯云 API 协议，逐字保留。
        # SampleRate 钳制（944eb4d 硬约束）：标准音色仅支持 8000/16000，
        # 直接传 conn.sample_rate(24000) 触发 InvalidParameterValue 致整段静音，
        # 故上限钳 16000，下游 pydub 重采样回设备率，对播放无影响。
        payload = {
            "Text": text,
            "SessionId": str(uuid.uuid4()),
            "VoiceType": self.voice,
            "Codec": self.audio_file_type,
            "Volume": self.volume,
            # speed=None → 实例默认（含 config/ttsRate 链）；显式传值（含 0.0）
            # 按 per-call 覆盖。附录 A 角色出厂语速 ±0.5 经此透传。
            "Speed": self.speed if speed is None else self._norm_speed(speed),
            "SampleRate": self._resolve_sample_rate(),
        }

        try:
            headers = self._sign_tencent_request(payload)
            # 演进点：原 requests.post 同步跑在事件循环上，腾讯 TTS 往返期间
            # 冻结全进程 WS/对讲/events；改为 to_thread 下沉 + 显式超时边界，
            # loop 持续响应。与 vllm(V1/V2)/R1 同源病灶修复。
            http_resp = await asyncio.to_thread(
                requests.post,
                self.api_url,
                json.dumps(payload),
                headers=headers,
                timeout=_REQUEST_TIMEOUT,
            )

            if http_resp.status_code != 200:
                raise RuntimeError(
                    f"{__name__} status_code: {http_resp.status_code} "
                    f"response: {http_resp.content}"
                )

            response_data = http_resp.json()
            error_info = response_data.get("Response", {}).get("Error")
            if error_info is not None:
                raise RuntimeError(
                    f"API返回错误: {error_info['Code']}: {error_info['Message']}"
                )

            audio_b64 = response_data.get("Response", {}).get("Audio")
            if not audio_b64:
                raise RuntimeError(f"{__name__}: 没有返回音频数据: {response_data}")

            audio_bytes = base64.b64decode(audio_b64)
            if output_file:
                with open(output_file, "wb") as f:
                    f.write(audio_bytes)
                return None
            return audio_bytes
        except Exception:
            # 保留原始异常类型（网络/超时/业务）与 traceback，不吞成通用 Exception，
            # 让上层 TTS 失败回退（EdgeTTS）能正确区分并降级。
            raise

    # ------------------------------------------------------------------
    # 流式文本语音合成（stream_wsv2 WebSocket）：边合成边回调 PCM，削首包延迟
    # ------------------------------------------------------------------
    async def text_to_stream(self, text, pcm_callback, speed=None):
        """腾讯云「流式文本语音合成」接口（wss stream_wsv2）。

        与一次性 TextToVoice 不同：本方法打开一个 WebSocket 会话，把整段文本
        以 ACTION_SYNTHESIS 发出，服务端按标点自动断句、边合成边回 binary PCM
        （16bit/单声道/SampleRate），逐块回调 pcm_callback 供调用方即时 opus
        编码下发 → 真正的流式首包（首段音频不必等整段合成完）。

        鉴权：HMAC-SHA1（区别于一次性接口用的 TC3-HMAC-SHA256）。
        失败一律抛异常，由上层 to_tts_stream 决定兜底/释放。
        """
        if not text or not text.strip():
            return None

        # ── 率对齐铁律（与 networked_reminder_scheduler 同源已验证范式）──
        # 腾讯流式返回的 PCM 采样率由 _resolve_sample_rate() 决定（标准/精品音色
        # 钳 16k、超自然大模型 24k），而下游 opus 编码器按 conn.sample_rate(24k)
        # 分帧；二者不等时若不重采样，16k 样本会被当 24k 帧长编码 → 设备按 24k
        # 解 → ~1.5x 快放+升调（与小程序非流式路径表现不一致）。故此处显式把
        # 腾讯返回的 PCM 升/降采样到 conn.sample_rate 再回调，复用 voicemail_player
        # 的 _resample_pcm（numpy 线性插值，容器内可用，缺失回退纯 Python）。
        _target_sr = self.conn.sample_rate if self.conn else 24000
        _actual_sr = self._resolve_sample_rate()
        if _actual_sr != _target_sr:
            from fuguoai_core.utils.voicemail_player import _resample_pcm

            def _resampled_cb(raw):
                if raw:
                    pcm_callback(_resample_pcm(raw, _actual_sr, _target_sr))
                # 空块直接丢弃：编码器末帧冲刷由 base.py 负责
        else:
            _resampled_cb = pcm_callback

        params = {
            "Action": "TextToStreamAudioWSv2",
            "AppId": int(self.appid),
            "SecretId": self.secret_id,
            "Timestamp": int(time.time()),
            "Expired": int(time.time()) + 86400,
            "SessionId": str(uuid.uuid4()),
            "VoiceType": self.voice,
            "SampleRate": self._resolve_sample_rate(),
            "Codec": "pcm",
            # 与 text_to_speak 同语义：None → 实例默认；显式值按 per-call 覆盖
            "Speed": self.speed if speed is None else self._norm_speed(speed),
            "Volume": self.volume,
        }
        keys = sorted(params)
        qs = "&".join(
            "%s=%s" % (k, urllib.parse.quote(str(params[k]), safe="")) for k in keys
        )
        # 签名原文：GET<host>/stream_wsv2?<按字典序排序且排除 Signature 的参数>
        sign_src = "GETtts.cloud.tencent.com/stream_wsv2?" + qs
        signature = base64.b64encode(
            hmac.new(self.secret_key.encode(), sign_src.encode(), hashlib.sha1).digest()
        ).decode()
        url = "wss://tts.cloud.tencent.com/stream_wsv2?%s&Signature=%s" % (
            qs,
            urllib.parse.quote(signature, safe=""),
        )

        async with websockets.connect(
            url, open_timeout=10, close_timeout=5, ping_interval=None, max_size=None
        ) as ws:
            # 握手：首帧应为 code==0
            msg = json.loads(await ws.recv())
            if msg.get("code") != 0:
                raise RuntimeError("Tencent流式TTS握手失败: %s" % msg)
            # 等待 READY 事件（ready==1）后方可发文本
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("ready") == 1:
                    break
                if msg.get("code", 0) != 0:
                    raise RuntimeError("Tencent流式TTS就绪失败: %s" % msg)
            # 发送合成文本 + 结束指令
            await ws.send(
                json.dumps(
                    {
                        "session_id": params["SessionId"],
                        "message_id": str(uuid.uuid4()),
                        "action": "ACTION_SYNTHESIS",
                        "data": text,
                    }
                )
            )
            await ws.send(
                json.dumps(
                    {
                        "session_id": params["SessionId"],
                        "message_id": str(uuid.uuid4()),
                        "action": "ACTION_COMPLETE",
                        "data": "",
                    }
                )
            )
            # 接收音频/事件，直到 FINAL
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=15)
                except asyncio.TimeoutError:
                    raise RuntimeError("Tencent流式TTS接收音频超时")
                if isinstance(raw, bytes):
                    if raw:
                        _resampled_cb(raw)
                else:
                    msg = json.loads(raw)
                    if msg.get("final") == 1:
                        break
                    if msg.get("code", 0) != 0:
                        raise RuntimeError("Tencent流式TTS合成失败: %s" % msg)
        return None
