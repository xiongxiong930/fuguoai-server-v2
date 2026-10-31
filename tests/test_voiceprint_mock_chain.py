# -*- coding: utf-8 -*-
"""声纹链路 mock 验证（不依赖真实腾讯云密钥/网络）— 供 tools/regression_gate.py L3 调用。

验证整条路径：
  identify_speaker → 腾讯云 1:N(VoicePrintGroupVerify, stub) → registry 落盘/回载
  → Connection._voiceprint_prompt_config(真实代码, ast 提取) 生成 'id,name,' 串
  → dialogue._render_dynamic_part(真实代码) 注入 <speakers_info>

做法：
- 用临时 FUGUOAI_DATA_DIR 隔离 registry 落盘；
- 把 provider._call 换成 async stub，按 action 返回官方格式的 canned 响应；
- 不 import 真实 connection 模块（其依赖 mcp 等重依赖），改用 ast 提取
  Connection._voiceprint_prompt_config 的源码独立执行，保证测的是真代码而非复刻。
"""
import os
import sys
import json
import asyncio
import tempfile
import shutil
import ast

# 必须在 import voiceprint_provider 之前设定：_REGISTRY_PATH 在 import 期
# 依据 FUGUOAI_DATA_DIR 计算。
_TMP = tempfile.mkdtemp(prefix="vp_mock_")
os.environ["FUGUOAI_DATA_DIR"] = _TMP

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import fuguoai_core.utils.voiceprint_provider as vp_mod  # noqa: E402
from fuguoai_core.utils.voiceprint_provider import (  # noqa: E402
    VoiceprintProvider,
    get_voiceprint_provider,
)
from fuguoai_core.utils.dialogue import Dialogue  # noqa: E402

PASS = 0


def ok(msg):
    global PASS
    PASS += 1
    print(f"  ✓ {msg}")


def _extract_voiceprint_prompt_config():
    # 真实 connection 模块依赖 mcp 等重依赖，无法直接 import；这里用 ast 提取
    # ConnectionHandler._voiceprint_prompt_config 的源码独立执行，保证测的是真代码。
    src = open(os.path.join(ROOT, "fuguoai_core", "connection.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef) \
                        and item.name == "_voiceprint_prompt_config":
                    mod = ast.Module(body=[item], type_ignores=[])
                    ns = {}
                    exec(compile(mod, "connection.py", "exec"), ns)
                    return ns["_voiceprint_prompt_config"]
    raise RuntimeError("未找到 _voiceprint_prompt_config")


_voiceprint_prompt_config = _extract_voiceprint_prompt_config()


class FakeCloud:
    """按 action 返回官方结构 canned 响应；并记录调用次数与 action。"""
    def __init__(self, group_verify=None):
        self.calls = []
        self.group_verify = group_verify or {
            "Data": {
                "VerifyTops": [
                    {"Score": "85", "SpeakerId": "爸爸", "VoicePrintId": "vp_爸爸"}
                ]
            }
        }

    async def call(self, action, params, timeout_s=10.0):
        self.calls.append(action)
        if action == "VoicePrintEnroll":
            name = params["SpeakerNick"]
            return {"Data": {"VoicePrintId": f"vp_{name}", "SpeakerNick": name}}
        if action == "VoicePrintGroupVerify":
            return self.group_verify
        raise AssertionError(f"unexpected action {action}")


def _make_provider(fake):
    cfg = {
        "provider": "tencent",
        "secret_id": "AKID_MOCK",
        "secret_key": "MOCK_SECRET",
        "group_id": "fuguoai_family_01",
        "similarity_threshold": 70,
    }
    p = VoiceprintProvider(cfg)
    p._call = fake.call.__get__(p, VoiceprintProvider)
    return p


# ───────────────────────── 用例 ─────────────────────────

def test_no_requests_dependency():
    assert not hasattr(vp_mod, "requests"), "R2 不变式：不得 import 同步 requests"
    ok("模块无 requests 依赖")


def test_get_voiceprint_provider_semantics():
    vp_mod._provider_cache.clear()
    none_p = get_voiceprint_provider({"voiceprint": {"provider": "tencent"}})
    assert none_p is None
    enabled_p = get_voiceprint_provider({
        "voiceprint": {
            "provider": "tencent",
            "secret_id": "AKID_MOCK",
            "secret_key": "MOCK_SECRET",
            "group_id": "fuguoai_family_01",
        }
    })
    assert enabled_p is not None and enabled_p.enabled
    ok("get_voiceprint_provider：缺密钥返回 None，齐全返回 enabled")


def test_enroll_and_persist():
    fake = FakeCloud()
    p = _make_provider(fake)
    res = asyncio.run(p.enroll_speaker("爸爸", b"FAKE_WAV_BYTES"))
    assert res["ok"] is True
    assert res["voiceprint_id"] == "vp_爸爸"
    assert res["name"] == "爸爸"
    reg_path = os.path.join(_TMP, "voiceprint_registry.json")
    assert os.path.exists(reg_path)
    with open(reg_path, encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["group_id"] == "fuguoai_family_01"
    assert data["speakers"][0]["name"] == "爸爸"
    assert data["speakers"][0]["voiceprint_id"] == "vp_爸爸"
    assert fake.calls == ["VoicePrintEnroll"]
    ok("enroll：注册成功 + registry 落盘 + 仅 1 次 Enroll 调用")


def test_persistence_reload():
    vp_mod._provider_cache.clear()
    p2 = VoiceprintProvider({
        "provider": "tencent",
        "secret_id": "AKID_MOCK",
        "secret_key": "MOCK_SECRET",
        "group_id": "fuguoai_family_01",
    })
    assert p2.enabled
    names = [s["name"] for s in p2.speakers]
    assert "爸爸" in names
    ok("落盘回载：新实例从 disk 恢复登记")


def test_identify_success_reverse_lookup():
    fake = FakeCloud()
    p = _make_provider(fake)
    name = asyncio.run(p.identify_speaker(b"FAKE_WAV_BYTES", "sess1"))
    assert name == "爸爸"
    assert "VoicePrintGroupVerify" in fake.calls
    ok("identify：1:N 命中并经 VoicePrintId 反查到 registry 名")


def test_identify_below_threshold():
    fake = FakeCloud(group_verify={
        "Data": {"VerifyTops": [
            {"Score": "30", "SpeakerId": "爸爸", "VoicePrintId": "vp_爸爸"}
        ]}
    })
    p = _make_provider(fake)
    name = asyncio.run(p.identify_speaker(b"FAKE_WAV_BYTES", "sess2"))
    assert name == "未知说话人"
    ok("identify：低于阈值 → 未知说话人")


def test_identify_empty_registry_no_network():
    fake = FakeCloud()
    p = VoiceprintProvider({
        "provider": "tencent",
        "secret_id": "AKID_MOCK",
        "secret_key": "MOCK_SECRET",
        "group_id": "fuguoai_family_01",
    })
    p.speakers = []
    p._call = fake.call.__get__(p, VoiceprintProvider)
    res = asyncio.run(p.identify_speaker(b"x", "sess3"))
    assert res is None
    assert fake.calls == [], "空 registry 不应发起任何腾讯云调用"
    ok("identify：空 registry → None 且零网络调用")


def test_prompt_config_format_and_dialogue_injection():
    class FakeConn:
        def __init__(self, vp):
            self.config = {"voiceprint": {"provider": "tencent"}}
            self.voiceprint_provider = vp

    p = VoiceprintProvider({
        "provider": "tencent",
        "secret_id": "AKID_MOCK",
        "secret_key": "MOCK_SECRET",
        "group_id": "fuguoai_family_01",
    })
    p.speakers = [
        {"voiceprint_id": "vp_爸爸", "name": "爸爸", "created_at": "t"},
        {"voiceprint_id": "vp_妈妈", "name": "妈妈", "created_at": "t"},
    ]

    cfg = _voiceprint_prompt_config(FakeConn(p))
    speakers = cfg.get("speakers")
    assert speakers is not None
    # 格式须与 connection.py:1217 完全一致：'voiceprint_id,name,'
    assert speakers == ["vp_爸爸,爸爸,", "vp_妈妈,妈妈,"]
    ok("prompt_config：生成 'id,name,' 串（与 connection.py:1217 一致）")

    d = Dialogue()
    template = (
        "你是复果AI家庭助手。\n"
        "<memory>用户偏好简短回答</memory>\n"
        "当前时间 {{current_time}}。"
    )
    out = d._render_dynamic_part(template, "用户偏好简短回答", cfg)
    assert "<speakers_info>" in out
    assert "</speakers_info>" in out
    assert "- 爸爸：" in out
    assert "- 妈妈：" in out
    assert "用户偏好简短回答" in out
    out2 = d._render_dynamic_part(template, "用户偏好简短回答", cfg)
    assert out2 == out  # memo 缓存等价
    ok("dialogue 注入：<speakers_info> 含 爸爸/妈妈 + 记忆替换 + memo 缓存")


def test_prompt_config_disabled_returns_base():
    class FakeConn:
        def __init__(self, vp):
            self.config = {"voiceprint": {"provider": "tencent"}}
            self.voiceprint_provider = vp

    p = VoiceprintProvider({
        "provider": "tencent",
        "secret_id": "",
        "secret_key": "",
        "group_id": "",
    })
    assert p.enabled is False
    cfg = _voiceprint_prompt_config(FakeConn(p))
    assert "speakers" not in cfg
    ok("prompt_config：禁用时不注入 speakers")


def main():
    print("=== 声纹 mock 链路验证 ===")
    try:
        test_no_requests_dependency()
        test_get_voiceprint_provider_semantics()
        test_enroll_and_persist()
        test_persistence_reload()
        test_identify_success_reverse_lookup()
        test_identify_below_threshold()
        test_identify_empty_registry_no_network()
        test_prompt_config_format_and_dialogue_injection()
        test_prompt_config_disabled_returns_base()
    except AssertionError as e:
        print(f"\nFAIL ❌ (after {PASS} checks): {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"\nERROR ❌ (after {PASS} checks): {type(e).__name__}: {e}")
        return 1
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
    print(f"\nALL {PASS} CHECKS PASS ✅ (voiceprint mock chain)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
