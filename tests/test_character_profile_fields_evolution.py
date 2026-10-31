# -*- coding: utf-8 -*-
"""线 0 专项（2026-10-16）：CharacterProfile 契约字段映射与附录 A 三字段落地。

锁三件事（后端对接文档 §4.1，159 只读实测结论）：
① voice_type↔voice、background↔backstory 双名兼容（yaml 旧字段不改，输出别名）
② speed/factory_sliders 缺省 None 不炸；transition_line 缺省落共用兜底句（附录 A）
③ to_dict 新旧字段并存（向后兼容：现有调用方读 voice/backstory 不破）

不含：speed→腾讯 TTS Speed（整数 -2~+6）的换算——无官方依据，属联调项，
故意不在读取层猜映射（纪律：不编造未文档化假设）。
"""

from fuguoai_core.utils.character_store import (
    DEFAULT_TRANSITION_LINE,
    DEFAULT_VOICE_BY_GENDER,
    CharacterProfile,
)


def _profile(raw):
    base = {"character_id": "t-001", "name": "测试"}
    base.update(raw)
    return CharacterProfile(base)


def test_alias_when_yaml_uses_old_names():
    """159 现盘形态：yaml 只有 voice/backstory → 别名同值。"""
    p = _profile({"voice": "502001", "backstory": "背景故事三段式"})
    assert p.voice_type == "502001"
    assert p.background == "背景故事三段式"
    # 旧字段保留（向后兼容）
    assert p.voice == "502001"
    assert p.backstory == "背景故事三段式"


def test_contract_names_take_precedence():
    """将来 admin 若把 yaml 改成契约名：契约名优先。"""
    p = _profile({"voice": "502001", "voice_type": "601011",
                  "backstory": "旧", "background": "新"})
    assert p.voice_type == "601011"
    assert p.background == "新"


def test_voice_type_falls_back_to_gender_default():
    """voice/voice_type 都缺 → 性别默认声线（与 self.voice 既有回退一致）。"""
    p = _profile({"gender": "female"})
    assert p.voice_type == DEFAULT_VOICE_BY_GENDER["female"]
    p2 = _profile({"gender": "male"})
    assert p2.voice_type == DEFAULT_VOICE_BY_GENDER["male"]


def test_appendix_a_fields_defaults():
    """天使心旧角色未配三字段：speed/sliders=None、过渡自语落兜底句，不炸。"""
    p = _profile({})
    assert p.speed is None
    assert p.factory_sliders is None
    assert p.transition_line == DEFAULT_TRANSITION_LINE
    assert p.transition_line == "……好像睡了很久。"


def test_appendix_a_fields_from_yaml():
    """附录 A 精确值入 yaml 后原样透出（值不在代码里臆造）。"""
    p = _profile({
        "speed": -0.5,
        "factory_sliders": {"温度": 55, "活力": 35, "调皮": 10},
        "transition_line": "睡了很久。等你的时候，不算长。",
    })
    assert p.speed == -0.5
    assert p.factory_sliders == {"温度": 55, "活力": 35, "调皮": 10}
    assert p.transition_line == "睡了很久。等你的时候，不算长。"


def test_to_dict_outputs_both_old_and_new_fields():
    p = _profile({"voice": "502001", "backstory": "背景", "speed": 0.5,
                  "factory_sliders": {"温度": 50}, "transition_line": "嘿——找到你了。"})
    d = p.to_dict()
    # 新（契约）字段
    assert d["voice_type"] == "502001"
    assert d["background"] == "背景"
    assert d["speed"] == 0.5
    assert d["factory_sliders"] == {"温度": 50}
    assert d["transition_line"] == "嘿——找到你了。"
    # 旧字段仍在（现有调用方不破）
    assert d["voice"] == "502001"
    assert d["backstory"] == "背景"
