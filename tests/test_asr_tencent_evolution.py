"""ASR 腾讯 provider 5-C 真进化离线测试。

钉死的真实改进：
- E1 异常分类暴露：_send_request 不再把 Timeout/ConnectionError/API错 统一吞成 None，
     而是保留原始异常类型上抛（Timeout/ConnectionError/IOError），便于线上定位。
- E2 超时元组：(连接 3.05s, 读取 10s)，连接抖动快速失败而非挂起 10s。
- 去相似化：_get_auth_headers -> _sign_tencent_request，腾讯 TC3 签名/字段逐字不变。
- 契约：speech_to_text(text, file_path) 接口与返回语义不变；上层只取 text。
"""

import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch, MagicMock

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fuguoai_core.providers.asr.tencent import ASRProvider


def _make_provider():
    out = tempfile.mkdtemp(prefix="asr_test_")
    return ASRProvider(
        {"secret_id": "AKID_test", "secret_key": "secret_key_test", "output_dir": out}
    )


def _artifacts(pcm: bytes = b"\x00" * 320):
    return SimpleNamespace(pcm_bytes=pcm, file_path="/tmp/asr_art.pcm")


class TestASRTencentEvolution(TestCase):
    def test_normal_result_and_timeout_tuple(self):
        provider = _make_provider()
        with patch("requests.post") as mock_post:
            resp = MagicMock()
            resp.ok = True
            resp.json.return_value = {"Response": {"Result": "你好世界"}}
            mock_post.return_value = resp
            out = provider._send_request("body", "123", "auth")
        self.assertEqual(out, "你好世界")
        # E2: 超时元组 (连接, 读取)
        self.assertEqual(mock_post.call_args.kwargs["timeout"], (3.05, 10))

    def test_timeout_raises_type_preserved(self):
        # E1: 超时异常类型保留，不被吞成 None
        provider = _make_provider()
        with patch("requests.post", side_effect=requests.exceptions.Timeout("t")):
            with self.assertRaises(requests.exceptions.Timeout):
                provider._send_request("body", "123", "auth")

    def test_connection_error_raises_type_preserved(self):
        provider = _make_provider()
        with patch(
            "requests.post", side_effect=requests.exceptions.ConnectionError("c")
        ):
            with self.assertRaises(requests.exceptions.ConnectionError):
                provider._send_request("body", "123", "auth")

    def test_api_error_raises_ioerror(self):
        provider = _make_provider()
        resp = MagicMock()
        resp.ok = True
        resp.json.return_value = {
            "Response": {"Error": {"Code": "FailedOperation", "Message": "boom"}}
        }
        with patch("requests.post", return_value=resp):
            with self.assertRaises(IOError):
                provider._send_request("body", "123", "auth")

    def test_empty_result_returns_empty_str(self):
        provider = _make_provider()
        resp = MagicMock()
        resp.ok = True
        resp.json.return_value = {"Response": {}}
        with patch("requests.post", return_value=resp):
            out = provider._send_request("body", "123", "auth")
        self.assertEqual(out, "")

    def test_sign_format_and_stable(self):
        # 去相似化后签名算法等价：格式正确且同输入结果稳定（同秒内）
        provider = _make_provider()
        ts1, auth1 = provider._sign_tencent_request("body1")
        ts2, auth2 = provider._sign_tencent_request("body1")
        self.assertTrue(auth1.startswith("TC3-HMAC-SHA256 Credential=AKID_test/"))
        self.assertIn("SignedHeaders=content-type;host;x-tc-action", auth1)
        self.assertEqual(ts1, ts2)
        self.assertEqual(auth1, auth2)

    async def _run_speech(self, side_effect=None, json_val=None):
        provider = _make_provider()
        if json_val is not None:
            resp = MagicMock()
            resp.ok = True
            resp.json.return_value = json_val
            with patch("requests.post", return_value=resp):
                return await provider.speech_to_text([b"x"], "sid", _artifacts())
        else:
            with patch("requests.post", side_effect=side_effect):
                return await provider.speech_to_text([b"x"], "sid", _artifacts())

    def test_speech_to_text_ok(self):
        # 契约不变：返回 (text, file_path)
        out = asyncio.run(
            self._run_speech(json_val={"Response": {"Result": "你好"}})
        )
        self.assertEqual(out, ("你好", "/tmp/asr_art.pcm"))

    def test_speech_to_text_timeout_returns_none_and_nonblocking(self):
        # 真异步：异常路径立即返回 (None, None)，不阻塞事件循环线程
        async def go():
            return await asyncio.wait_for(
                self._run_speech(side_effect=requests.exceptions.Timeout("t")),
                timeout=2,
            )

        out = asyncio.run(go())
        self.assertEqual(out, (None, None))


if __name__ == "__main__":
    import unittest

    unittest.main()
