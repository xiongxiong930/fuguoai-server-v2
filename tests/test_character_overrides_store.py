# -*- coding: utf-8 -*-
"""角色调校覆盖层（users.json `character_overrides`）自进化测试。

2026-10-16 活字匣调校链路断层修复：前端 PG-TUNE 调校面板存的键名是
`voice_type`（腾讯 VoiceType 数字 ID），PUT preferences 白名单此前只收
旧键 `voice` → 调校音色根本进不了后端。本文件锁住该层行为（此前**零测试**，
属「改坏没人知道」区）：

  ① 白名单收 voice_type（新）与 voice（旧，天使心数据向后兼容）
  ② 白名单拒 admin 字段（system_prompt/gender/tier 等客户端写不进来）
  ③ 按 cid 合并语义：写 A 心不冲掉 B 心；同键更新
  ④ clear_character_override 真删除（set 只能增改，「一键重置」靠它）
"""

from fuguoai_core.utils.angel_heart_store import UserProfileStore


def _store(tmp_path):
    return UserProfileStore(str(tmp_path / "users.json"))


# ───────────────────────── ① 白名单收键 ─────────────────────────

def test_override_accepts_voice_type_key(tmp_path):
    """前端 PG-TUNE 存 voice_type，白名单必须收（断链修复的核心）。"""
    s = _store(tmp_path)
    s.set_preferences("u1", {"character_overrides": {
        "xiaoman-001": {"voice_type": "601011", "name": "小果"}}})
    ov = s.get_preferences("u1")["character_overrides"]["xiaoman-001"]
    assert ov["voice_type"] == "601011"
    assert ov["name"] == "小果"


def test_override_accepts_legacy_voice_key(tmp_path):
    """天使心旧数据用 voice 键，继续收（向后兼容，不破已落盘数据）。"""
    s = _store(tmp_path)
    s.set_preferences("u1", {"character_overrides": {
        "chuxue-001": {"voice": "1018"}}})
    ov = s.get_preferences("u1")["character_overrides"]["chuxue-001"]
    assert ov["voice"] == "1018"


# ───────────────────────── ② 白名单拒键 ─────────────────────────

def test_override_drops_non_whitelist_fields(tmp_path):
    """system_prompt/gender/tier 等归 admin，客户端写不进来。"""
    s = _store(tmp_path)
    s.set_preferences("u1", {"character_overrides": {
        "xiaoman-001": {
            "system_prompt": "hack", "gender": "女", "tier": "preset",
            "background": "x", "voice_type": "502001"}}})
    ov = s.get_preferences("u1")["character_overrides"]["xiaoman-001"]
    for f in ("system_prompt", "gender", "tier", "background"):
        assert f not in ov, f"非白名单字段泄入: {f}"
    assert ov["voice_type"] == "502001"   # 同包里的合法键不受牵连


# ───────────────────────── ③ 按 cid 合并 ─────────────────────────

def test_override_merges_per_character_not_replace(tmp_path):
    """按 cid 合并：写 A 心不冲掉 B 心（整块替换会丢其他心的调校）。"""
    s = _store(tmp_path)
    s.set_preferences("u1", {"character_overrides": {
        "xiaoman-001": {"voice_type": "502001"}}})
    s.set_preferences("u1", {"character_overrides": {
        "ache-002": {"voice_type": "601011"}}})
    ov = s.get_preferences("u1")["character_overrides"]
    assert ov["xiaoman-001"]["voice_type"] == "502001"   # A 心未被冲掉
    assert ov["ache-002"]["voice_type"] == "601011"


def test_override_updates_same_key(tmp_path):
    """同键更新（改音色再改回来）。"""
    s = _store(tmp_path)
    s.set_preferences("u1", {"character_overrides": {
        "xiaoman-001": {"voice_type": "502001"}}})
    s.set_preferences("u1", {"character_overrides": {
        "xiaoman-001": {"voice_type": "601011"}}})
    ov = s.get_preferences("u1")["character_overrides"]["xiaoman-001"]
    assert ov["voice_type"] == "601011"


# ───────────────────────── ④ 真删除 ─────────────────────────

def test_clear_character_override_removes(tmp_path):
    """「一键重置原装」走 clear 真删除；返回是否确有删除。"""
    s = _store(tmp_path)
    s.set_preferences("u1", {"character_overrides": {
        "xiaoman-001": {"voice_type": "502001"}}})
    assert s.clear_character_override("u1", "xiaoman-001") is True
    assert "xiaoman-001" not in s.get_preferences("u1")["character_overrides"]
    # 第二次清（已无覆盖）返回 False，不误报
    assert s.clear_character_override("u1", "xiaoman-001") is False
