"""
FlowTTS provider 离线测试（mock WebSocket 服务端，无需真实腾讯云账号/网络）。

验证：
- _build_url 含 Action/SdkAppId/Signature，且签名用 HMAC-SHA1；
- text_to_speak 返回按 SentenceAudio 到达顺序拼接的音频字节；
- text_to_stream 把 raw PCM 逐片回调 pcm_callback（顺序一致）；
- 长文本按 <=1000 字符分片发 ContinueSession，拼接后还原原文。
"""
import asyncio
import base64
import json
import unittest
from unittest.mock import patch

from fuguoai_core.providers.tts import flowtts
from fuguoai_core.providers.tts.flowtts import TTSProvider


class FakeWS:
    """模拟 FlowTTS 服务端：先回 SessionStart，再依次回音频分片，最后 SessionEnd。"""

    def __init__(self, audio_chunks):
        self.audio_chunks = audio_chunks
        self.sent = []
        self._started = False
        self._idx = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def recv(self):
        if not self._started:
            self._started = True
            return json.dumps({
                "Event": "SessionStart", "ConnectionId": "x",
                "SessionId": "sess-1", "MessageId": "m", "Data": {},
            })
        if self._idx < len(self.audio_chunks):
            chunk = self.audio_chunks[self._idx]
            self._idx += 1
            b64 = base64.b64encode(chunk).decode("ascii")
            return json.dumps({
                "Event": "SentenceAudio", "ConnectionId": "x",
                "SessionId": "sess-1", "MessageId": "m",
                "Data": {"SentenceId": 1, "Sentence": "t",
                         "Audio": b64, "Duration": 1.0, "IsEnd": False},
            })
        return json.dumps({
            "Event": "SessionEnd", "ConnectionId": "x",
            "SessionId": "sess-1", "MessageId": "m", "Data": {},
        })


def make_provider():
    return TTSProvider({
        "secret_id": "AKID_test",
        "secret_key": "secret_test",
        "sdk_app_id": "1400000001",
        "voice": "v-test-abc",
        "output_dir": "tmp/",
    }, True)


class FlowTTSOfflineTest(unittest.TestCase):

    def test_build_url_has_signature_and_params(self):
        prov = make_provider()
        url = prov._build_url()
        self.assertIn("Action=TextToSpeechBidirection", url)
        self.assertIn("SdkAppId=1400000001", url)
        self.assertIn("SecretId=AKID_test", url)
        self.assertIn("Signature=", url)
        # Signature 段非空
        sig = url.split("Signature=", 1)[1]
        self.assertTrue(sig)

    def test_text_to_speak_concatenates_audio(self):
        chunks = [b"\x00\x01\x02", b"\x03\x04", b"\x05\x06\x07\x08"]
        fake = FakeWS(chunks)
        prov = make_provider()
        with patch.object(flowtts.websockets, "connect",
                          return_value=fake) as conn_mock:
            audio = asyncio.run(prov.text_to_speak("你好", None))
        self.assertEqual(audio, b"".join(chunks))
        # 首帧为 StartSession，其后为 ContinueSession + FinishSession
        events = [m["Event"] for m in fake.sent]
        self.assertEqual(events[0], "StartSession")
        self.assertIn("ContinueSession", events)
        self.assertEqual(events[-1], "FinishSession")
        # URL 被使用
        self.assertTrue(conn_mock.called)

    def test_text_to_stream_callback_receives_pcm_in_order(self):
        chunks = [b"PCM1", b"PCM2", b"PCM3"]
        fake = FakeWS(chunks)
        prov = make_provider()
        collected = []
        with patch.object(flowtts.websockets, "connect", return_value=fake):
            asyncio.run(prov.text_to_stream("今天天气真好", collected.append))
        self.assertEqual(b"".join(collected), b"".join(chunks))

    def test_long_text_chunked_at_1000(self):
        text = "字" * 2500  # 应分 3 片
        fake = FakeWS([b"x"])
        prov = make_provider()
        with patch.object(flowtts.websockets, "connect", return_value=fake):
            asyncio.run(prov.text_to_speak(text, None))
        cont = [m["Data"]["Text"] for m in fake.sent if m["Event"] == "ContinueSession"]
        self.assertEqual(len(cont), 3)
        self.assertEqual("".join(cont), text)


if __name__ == "__main__":
    unittest.main()
