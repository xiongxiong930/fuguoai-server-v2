"""M15 角色预设模板库（后端）逻辑验证。在容器内 /opt/fuguoai-server 环境下运行：

    python3 test_m15_presets.py

覆盖：
  Part1 目录：get_all_presets 返回全部人格（当前 8 套，id/name/description/emoji，不含完整 prompt）
  Part2 单条：get_preset / preset_config 返回完整人格包；各预设均绑定 EdgeTTS + 音色
  Part3 字段校验：_prepare_apply_fields 的范围/长度/None/空串 跳过逻辑
  Part4 端到端套用：apply 写入设备 agent-config + PATCH 合并保留未传字段

说明：纯逻辑级验证，不依赖固件/小程序/外部鉴权，可独立运行（固件重写、小程序审核期间
      后端自测用）。与 test_m7.py 风格一致，由 run_tests.sh 自动发现并执行。
"""
import os
import sys

sys.path.insert(0, "/opt/fuguoai-server")

from fuguoai_core.utils.preset_store import get_all_presets, get_preset, preset_config
from fuguoai_core.api import preset_routes as pr
from fuguoai_core.utils.agent_config_store import get_agent_config_store

_EXPECTED_IDS = {
    "storytelling", "english_teacher", "fitness_coach", "calm_listener",
    "serious_assistant", "cute_companion", "pro_advisor", "witty_roast",
}

# 预设应绑定的 Edge 音色（角色音色分离）
_EXPECTED_VOICE = {
    "storytelling": "zh-CN-XiaoxiaoNeural",
    "english_teacher": "en-US-AriaNeural",
    "fitness_coach": "zh-CN-YunxiNeural",
    "calm_listener": "zh-CN-XiaoyiNeural",
    "serious_assistant": "zh-CN-YunyangNeural",
    "cute_companion": "zh-CN-XiaoxiaoNeural",
    "pro_advisor": "zh-CN-YunxiNeural",
    "witty_roast": "zh-CN-XiaoyiNeural",
}


def test_catalog():
    presets = get_all_presets()
    # 预设目录会随产品迭代扩充，断言下限而非写死精确数量，避免陈旧断言误杀
    assert len(presets) >= 8, presets
    ids = {p["id"] for p in presets}
    assert _EXPECTED_IDS <= ids, ids  # 既有 8 套必须全部在场
    for p in presets:
        assert set(p.keys()) == {"id", "name", "description", "emoji"}, p.keys()
        assert "prompt" not in p, "目录不应含完整 prompt（体积控制）"
        assert p["emoji"], "emoji 缺失"
    print(f"[P1] catalog ok: {len(presets)} 套人格到场，字段合规，目录不含完整 prompt")


def test_single():
    for pid in _EXPECTED_IDS:
        full = get_preset(pid)
        assert full and full["config"]["prompt"], pid
        cfg = preset_config(pid)
        assert isinstance(cfg, dict) and cfg["prompt"]
        fields = pr._prepare_apply_fields(cfg)
        assert fields.get("prompt"), pid
        assert fields.get("speed") is not None, pid
        # 音色已绑定：tts_provider=EdgeTTS + 各自 Edge voice
        assert fields.get("tts_provider") == "EdgeTTS", pid
        assert fields.get("voice") == _EXPECTED_VOICE[pid], (pid, fields.get("voice"))
    # 不存在返回 None
    assert get_preset("nope") is None
    assert preset_config("nope") is None
    print("[P2] single ok: 4 套完整人格包 + 各绑定 EdgeTTS 音色")


def test_prepare_validation():
    # None 跳过（音色）
    assert pr._prepare_apply_fields({"tts_provider": None, "voice": None}) == {}
    # speed 范围 0.5–2.0
    assert pr._prepare_apply_fields({"speed": 1.3}) == {"speed": 1.3}
    assert "speed" not in pr._prepare_apply_fields({"speed": 0.4}), "speed<0.5 跳过"
    assert "speed" not in pr._prepare_apply_fields({"speed": 2.1}), "speed>2.0 跳过"
    # volume 范围 0.0–1.0
    assert pr._prepare_apply_fields({"volume": 0.7}) == {"volume": 0.7}
    assert "volume" not in pr._prepare_apply_fields({"volume": -0.1})
    assert "volume" not in pr._prepare_apply_fields({"volume": 1.1})
    # wakeup_words ≤100
    assert "wakeup_words" not in pr._prepare_apply_fields({"wakeup_words": "x" * 101})
    assert pr._prepare_apply_fields({"wakeup_words": "x" * 100}) == {"wakeup_words": "x" * 100}
    # prompt ≤4000
    assert "prompt" not in pr._prepare_apply_fields({"prompt": "x" * 4001})
    # tone/dialect 空串跳过
    assert "tone" not in pr._prepare_apply_fields({"tone": ""})
    assert "dialect" not in pr._prepare_apply_fields({"dialect": ""})
    # 真实预设的 speed/volume 合法
    for pid in _EXPECTED_IDS:
        f = pr._prepare_apply_fields(preset_config(pid))
        assert 0.5 <= f["speed"] <= 2.0, (pid, f["speed"])
        assert 0.0 <= f["volume"] <= 1.0, (pid, f["volume"])
    print("[P3] prepare ok: None/范围/长度/空串 校验正确")


def test_apply_and_merge():
    store = get_agent_config_store()
    dev = "TEST-M15-DEV"
    store.delete_config(dev)
    try:
        # 1) 套用 english_teacher
        cfg = preset_config("english_teacher")
        store.put_config(dev, pr._prepare_apply_fields(cfg), user_id="TEST")
        r1 = store.get_config(dev)
        assert r1["prompt"] == cfg["prompt"], "prompt 未写入"
        assert r1["speed"] == 1.0 and r1["volume"] == 1.0
        assert r1["tone"] == "清晰" and r1["dialect"] == "英语"
        assert r1.get("tts_provider") == "EdgeTTS" and r1.get("voice") == "en-US-AriaNeural", "英语老师音色未写入"

        # 2) PATCH 合并：仅改 speed/volume，未传字段(prompt/tone/dialect)必须保留
        store.put_config(dev, {"speed": 1.5, "volume": 0.7}, user_id="TEST")
        r2 = store.get_config(dev)
        assert r2["prompt"] == cfg["prompt"], "PATCH 合并丢失未传的 prompt"
        assert r2["speed"] == 1.5 and r2["volume"] == 0.7
        assert r2["tone"] == "清晰" and r2["dialect"] == "英语", "PATCH 合并丢失 tone/dialect"

        # 3) 再套用 storytelling：覆盖 prompt/speed/音色（切到晓晓）
        cfg2 = preset_config("storytelling")
        store.put_config(dev, pr._prepare_apply_fields(cfg2), user_id="TEST")
        r3 = store.get_config(dev)
        assert r3["prompt"] == cfg2["prompt"], "套用未覆盖 prompt"
        assert r3["speed"] == 0.9 and r3["volume"] == 0.9
        assert r3["tone"] == "温柔" and r3["dialect"] == "普通话"
        assert r3.get("tts_provider") == "EdgeTTS" and r3.get("voice") == "zh-CN-XiaoxiaoNeural", "切换角色后音色未更新为晓晓"
    finally:
        store.delete_config(dev)
    print("[P4] apply+merge ok: 套用写入 / PATCH 保留未传字段 / 切角色音色随之更新")


def test_active_preset_backref():
    """M15 收尾：套用预设回写 active_preset 反查；手动编辑(等价于 M12 PUT)清空该标记。"""
    store = get_agent_config_store()
    dev = "TEST-M15-ROLE"
    store.delete_config(dev)
    try:
        # 1) 套用预设（等价 handle_apply 写入 active_preset）
        cfg = preset_config("calm_listener")
        fields = pr._prepare_apply_fields(cfg)
        fields["active_preset"] = "calm_listener"
        store.put_config(dev, fields, user_id="TEST")
        r = store.get_config(dev)
        assert r.get("active_preset") == "calm_listener", f"active_preset 未回写: {r}"
        # 2) 手动编辑（等价 M12 PUT 置 active_preset=None）：标记清除，且 PATCH 合并保留其它字段
        store.put_config(dev, {"speed": 1.2, "active_preset": None}, user_id="TEST")
        r2 = store.get_config(dev)
        assert r2.get("active_preset") is None, f"手动编辑未清除 active_preset: {r2}"
        assert r2["speed"] == 1.2, "PATCH 合并应保留 speed"
        assert r2["voice"] == "zh-CN-XiaoyiNeural", "PATCH 合并应保留原音色"
    finally:
        store.delete_config(dev)
    print("[P5] active_preset ok: 套用回写反查 / 手动编辑清空 / PATCH 合并保留")


if __name__ == "__main__":
    test_catalog()
    test_single()
    test_prepare_validation()
    test_apply_and_merge()
    test_active_preset_backref()
    print("\nM15 PRESETS VERIFY ALL OK")
