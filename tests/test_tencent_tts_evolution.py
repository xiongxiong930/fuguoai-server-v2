import asyncio
import base64
import json
import os
import sys
import time
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fuguoai_core.providers.tts.tencent as tencent_mod
from fuguoai_core.providers.tts.tencent import TTSProvider


def make_provider(sample_rate=24000):
    cfg = {
        "appid": "appid",
        "secret_id": "sid",
        "secret_key": "sk",
        "voice": "1005",
        "region": "ap-guangzhou",
        "format": "wav",
        "speed": "0",
        "volume": "0",
    }
    p = TTSProvider(cfg, delete_audio_file=False)
    p.conn = type("Conn", (), {"sample_rate": sample_rate})()
    return p


class TestTencentTTSEvolution(unittest.TestCase):
    def test_t1_normal_returns_audio_bytes(self):
        audio = b"\x00\x01test"
        resp = Mock(
            status_code=200,
            json=lambda: {"Response": {"Audio": base64.b64encode(audio).decode()}},
        )
        with patch.object(tencent_mod.requests, "post", return_value=resp):
            out = asyncio.run(make_provider().text_to_speak("你好", None))
        self.assertEqual(out, audio)

    def test_t2_timeout_preserves_loop_and_type(self):
        def slow_post(*a, **k):
            # 模拟阻塞发生在 worker 线程，不冻结事件循环
            time.sleep(0.3)
            raise tencent_mod.requests.exceptions.Timeout("mock timeout")

        async def run():
            p = make_provider()
            counter = {"n": 0}

            async def watchdog():
                for _ in range(40):
                    counter["n"] += 1
                    await asyncio.sleep(0.01)

            wt = asyncio.create_task(watchdog())
            with self.assertRaises(tencent_mod.requests.exceptions.Timeout):
                await p.text_to_speak("hi", None)
            await wt
            self.assertGreater(counter["n"], 10, "loop froze during blocking TTS call")

        with patch.object(tencent_mod.requests, "post", slow_post):
            asyncio.run(run())

    def test_t3_api_error_raises_with_code(self):
        resp = Mock(
            status_code=200,
            json=lambda: {
                "Response": {"Error": {"Code": "InvalidParameter", "Message": "bad"}}
            },
        )
        with patch.object(tencent_mod.requests, "post", return_value=resp):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(make_provider().text_to_speak("hi", None))
        self.assertIn("InvalidParameter", str(ctx.exception))

    def test_t4_no_audio_raises(self):
        resp = Mock(status_code=200, json=lambda: {"Response": {}})
        with patch.object(tencent_mod.requests, "post", return_value=resp):
            with self.assertRaises(RuntimeError):
                asyncio.run(make_provider().text_to_speak("hi", None))

    def test_t5_samplerate_clamped(self):
        captured = {}

        def fake_post(url, data, headers=None, timeout=None):
            captured["data"] = data
            return Mock(
                status_code=200,
                json=lambda: {"Response": {"Audio": base64.b64encode(b"x").decode()}},
            )

        with patch.object(tencent_mod.requests, "post", fake_post):
            asyncio.run(make_provider(24000).text_to_speak("hi", None))
        self.assertEqual(json.loads(captured["data"])["SampleRate"], 16000)

        with patch.object(tencent_mod.requests, "post", fake_post):
            asyncio.run(make_provider(8000).text_to_speak("hi", None))
        self.assertEqual(json.loads(captured["data"])["SampleRate"], 8000)

    def test_t6_exception_type_preserved(self):
        def conn_err(*a, **k):
            raise tencent_mod.requests.exceptions.ConnectionError("refused")

        with patch.object(tencent_mod.requests, "post", conn_err):
            with self.assertRaises(tencent_mod.requests.exceptions.ConnectionError):
                asyncio.run(make_provider().text_to_speak("hi", None))


if __name__ == "__main__":
    unittest.main()
