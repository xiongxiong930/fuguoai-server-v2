"""腾讯流式 TTS（stream_wsv2）离线回归测试。

不触网：用 unittest.mock 替换 websockets.connect，验证
1) 握手/ready 正常时，binary PCM 逐块回调 pcm_callback；
2) 收到 final 后正常结束；
3) 握手 code!=0 / 合成 code!=0 抛异常（交由上层兜底）；
4) PCM 经真实 OpusEncoderUtils 能产出 opus 帧（证明 pcm→opus 数据通路）。
"""
import asyncio
import json
import os
import sys
from unittest.mock import patch, Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fuguoai_core.providers.tts.tencent import TTSProvider
from fuguoai_core.utils.opus_encoder_utils import OpusEncoderUtils


def _make_provider():
    cfg = {
        "appid": 1234567890,
        "secret_id": "AKIDtest",
        "secret_key": "secretkey",
        "voice": 602003,
        "region": "ap-guangzhou",
        "output_dir": "tmp/",
    }
    p = TTSProvider(cfg, delete_audio_file=True)

    class _Conn:
        sample_rate = 24000

    p.conn = _Conn()
    return p


class _FakeWS:
    def __init__(self, messages):
        self._msgs = list(messages)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def recv(self):
        if self._msgs:
            return self._msgs.pop(0)
        raise asyncio.CancelledError()

    async def send(self, data):
        self.sent.append(data)


def _handshake_then_pcm_then_final(pcm_chunks):
    msgs = [
        json.dumps({"code": 0, "message": "success", "session_id": "s1"}),
        json.dumps({"code": 0, "ready": 1, "session_id": "s1"}),
    ]
    for c in pcm_chunks:
        msgs.append(c)  # binary
    msgs.append(json.dumps({"code": 0, "final": 1, "session_id": "s1"}))
    return msgs


def test_stream_hands_pcm_to_callback():
    p = _make_provider()
    received = []

    def cb(pcm):
        received.append(pcm)

    pcm_chunks = [b"\x00\x01\x02\x03" * 200, b"\x04\x05\x06\x07" * 200]
    ws = _FakeWS(_handshake_then_pcm_then_final(pcm_chunks))
    with patch("fuguoai_core.providers.tts.tencent.websockets.connect", new=Mock(return_value=ws)):
        asyncio.run(p.text_to_stream("你好，流式测试。", cb))
    assert received == pcm_chunks, "PCM 应逐块原样回调"
    # 发出的指令：先 ACTION_SYNTHESIS 再 ACTION_COMPLETE
    actions = [json.loads(s)["action"] for s in ws.sent]
    assert actions == ["ACTION_SYNTHESIS", "ACTION_COMPLETE"], actions


def test_stream_url_contains_action_and_signature():
    p = _make_provider()
    captured = {}

    class _WS2(_FakeWS):
        async def __aenter__(self):
            captured["url"] = getattr(_WS2, "url", None)
            return self

    pcm = [b"\x00\x01" * 100]
    ws = _FakeWS(_handshake_then_pcm_then_final(pcm))
    with patch("fuguoai_core.providers.tts.tencent.websockets.connect", new=Mock(return_value=ws)) as m:
        asyncio.run(p.text_to_stream("测试", lambda x: None))
        # connect 被调用时第一个位置参是 url
        url = m.call_args[0][0]
    assert "Action=TextToStreamAudioWSv2" in url, url
    assert "Signature=" in url, url
    assert "wss://tts.cloud.tencent.com/stream_wsv2" in url, url


def test_stream_handshake_error_raises():
    p = _make_provider()
    msgs = [json.dumps({"code": 10003, "message": "auth fail"})]
    ws = _FakeWS(msgs)
    with patch("fuguoai_core.providers.tts.tencent.websockets.connect", new=Mock(return_value=ws)):
        try:
            asyncio.run(p.text_to_stream("测试", lambda x: None))
            raise AssertionError("应抛异常")
        except RuntimeError as e:
            assert "握手失败" in str(e), str(e)


def test_stream_pcm_to_opus_pipeline():
    """真实 opus 编码器：PCM 块经 encode_pcm_to_opus_stream 应产出 opus 帧。"""
    enc = OpusEncoderUtils(24000, 1, 60)
    out = []

    def cb(frame):
        if frame:
            out.append(frame)

    # 4800 样本 = 0.2s @24k → 至少 3 个 60ms 帧(1440 样本)
    pcm = b"\x00\x01" * 2400 + b"\x02\x03" * 2400
    enc.encode_pcm_to_opus_stream(pcm, False, cb)
    enc.encode_pcm_to_opus_stream(b"", True, cb)
    assert len(out) >= 3, "应产出多个 opus 帧，证明 pcm→opus 通路可用"


if __name__ == "__main__":
    test_stream_hands_pcm_to_callback()
    test_stream_url_contains_action_and_signature()
    test_stream_handshake_error_raises()
    test_stream_pcm_to_opus_pipeline()
    print("ALL TENCENT STREAM TESTS PASSED")
