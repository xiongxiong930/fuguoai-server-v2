"""
声纹双用（VoiceClone）离线测试（mock 方法层，无需真实腾讯云账号/网络）。

直接替掉 provider 的两个网络方法（_call=说话人识别 enroll，_call_trtc=声音复刻），
避免触达真实 ASR/TRTC 接口，同时验证：
- clone_enabled=False 时 clone_voice 直接返回 None（不调网络）；
- clone_enabled=True 且 _call_trtc 返回 VoiceId 时，enroll_speaker 把 voice_id 落 registry
  并在返回 dict 中带 voice_id；
- 克隆异常（业务 Error / 网络异常）时 enroll_speaker 仍 ok、voice_id=None，不阻断注册；
- get_cloned_voice_id 能反查；
- VoiceName 仅保留数字/字母/下划线、<=36 位。
"""
import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock

import fuguoai_core.utils.voiceprint_provider as vp_mod
from fuguoai_core.utils.voiceprint_provider import VoiceprintProvider


def make_provider(clone_enabled=True, sdk_app_id="1400000001"):
    return VoiceprintProvider({
        "provider": "tencent",
        "secret_id": "AKID_test",
        "secret_key": "secret_test",
        "group_id": "fuguoai_family_test",
        "similarity_threshold": 70,
        "clone_enabled": clone_enabled,
        "sdk_app_id": sdk_app_id,
        "clone_region": "ap-guangzhou",
        "clone_model": "flow_02_turbo",
        "clone_language": "zh",
    })


class VoiceCloneOfflineTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig_reg = vp_mod._REGISTRY_PATH
        self._orig_data = vp_mod._DATA_DIR
        vp_mod._DATA_DIR = self._tmp
        vp_mod._REGISTRY_PATH = os.path.join(self._tmp, "vp.json")

    def tearDown(self):
        vp_mod._REGISTRY_PATH = self._orig_reg
        vp_mod._DATA_DIR = self._orig_data

    def _wire(self, prov, enroll_vp_id="vp-1", trtc_result=None, trtc_error=None):
        # enroll（说话人识别）永远成功返回 VoicePrintId
        prov._call = AsyncMock(return_value={"Data": {"VoicePrintId": enroll_vp_id}})
        if trtc_error is not None:
            prov._call_trtc = AsyncMock(side_effect=trtc_error)
        else:
            prov._call_trtc = AsyncMock(return_value=trtc_result or
                                       {"Data": {"VoiceId": "v-abc-123"}})

    def test_clone_disabled_returns_none(self):
        prov = make_provider(clone_enabled=False)
        self.assertFalse(prov.clone_enabled)
        self.assertIsNone(asyncio.run(prov.clone_voice("爸爸", b"WAVDATA")))

    def test_enroll_stores_voice_id_on_clone_success(self):
        prov = make_provider(clone_enabled=True)
        self._wire(prov, trtc_result={"Data": {"VoiceId": "v-abc-123"}})
        result = asyncio.run(prov.enroll_speaker("爸爸", b"WAVDATA"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["voice_id"], "v-abc-123")
        # 内存反查
        self.assertEqual(prov.get_cloned_voice_id("爸爸"), "v-abc-123")
        # 落盘文件反查
        with open(vp_mod._REGISTRY_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["speakers"][0]["voice_id"], "v-abc-123")

    def test_enroll_ok_when_clone_fails(self):
        prov = make_provider(clone_enabled=True)
        self._wire(prov, trtc_result={"Response": {"Error": {
            "Code": "FailedOperation", "Message": "audio too short"}}})
        result = asyncio.run(prov.enroll_speaker("妈妈", b"SHORT"))
        self.assertTrue(result["ok"])
        self.assertIsNone(result["voice_id"])

    def test_enroll_ok_when_clone_network_error(self):
        prov = make_provider(clone_enabled=True)
        self._wire(prov, trtc_error=RuntimeError("network down"))
        result = asyncio.run(prov.enroll_speaker("哥哥", b"WAVDATA"))
        self.assertTrue(result["ok"])
        self.assertIsNone(result["voice_id"])

    def test_sanitize_clone_name(self):
        import re as _re
        # 中文仅名字：非字母数字下划线被剥光 → 落到 voice_ UUID 兜底，仍满足约束
        cn = VoiceprintProvider._sanitize_clone_name("爸爸")
        self.assertTrue(_re.fullmatch(r"[0-9A-Za-z_]+", cn))
        self.assertLessEqual(len(cn), 36)
        self.assertEqual(VoiceprintProvider._sanitize_clone_name("Hello World!"),
                         "Hello_World_")
        long = "a" * 50
        self.assertEqual(len(VoiceprintProvider._sanitize_clone_name(long)), 36)


if __name__ == "__main__":
    unittest.main()
