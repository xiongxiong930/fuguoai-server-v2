# -*- coding: utf-8 -*-
"""天使心「角色分层可见性闸门」自进化测试（纯单元，不依赖运行服务器）。

对应自进化方向 A2：`GET /characters` 对 owned 未持卡角色返回 `locked: true`
且**脱敏 system_prompt**（commit `c6620a1` 引入的行为）此前**无任何测试锁**，
属"改坏没人知道"的高风险区——这是"看得到、用不了"产品口径的实现处。

覆盖：
  ① _visible_to：preset 人人可见 / owned 需持卡 / 未持卡不可见 / None 不可见
  ② handle_list_characters：owned 未持卡 → locked=true 且**白名单构造**——
     契约 §2.1 禁发字段（system_prompt/voice_type/speed/background/
     personality/backstory 等）一个都不下发；持卡 → owned=true、
     locked=false、完整人格正常下发；preset → 一律可见不锁
  ③ handle_get_character：owned 未持卡 → 404（详情一律拦，不只目录）

  2026-10-16 扩：生产 E2E 实证旧实现只 pop 了 system_prompt，
  voice_type/speed/background/personality 四字段仍泄露（违背契约 §2.1）→
  fixture to_dict 补全字段集 + locked 分支逐字段禁发断言锁死。
"""

import json

import pytest

from fuguoai_core.utils.character_store import TIER_PRESET, TIER_OWNED

# 契约 §2.1：locked 角色禁发字段（人格/调校/LLM 素材，2026-10-16 E2E 实证
# 旧实现只脱敏 system_prompt，voice_type/speed/background/personality 泄露）。
# ⚠️ background 与 backstory 在 CharacterProfile 里同值，两个都要禁。
_FORBIDDEN_LOCKED_FIELDS = (
    "system_prompt", "voice_type", "speed", "background", "backstory",
    "personality", "transition_line", "factory_sliders", "awaken_quote",
    "led_config", "awaken_sound", "voice", "tags", "age", "product",
)

# locked 允许字段 = 白名单 + 分层标志（前端 locked 卡渲染/判定依赖）
_EXPECTED_LOCKED_KEYS = {"character_id", "name", "avatar", "gender", "tier", "owned", "locked"}


class _Profile:
    def __init__(self, cid, tier, name):
        self.character_id = cid
        self.tier = tier
        self.name = name
        self.system_prompt = "SP-" + cid

    def to_dict(self):
        # 镜像真实 CharacterProfile.to_dict() 全字段集（2026-10-16 补全）：
        # 早期 fixture 只有 4 字段，「locked 分支泄人格字段」这类回归锁不住。
        return {
            "character_id": self.character_id,
            "name": self.name,
            "gender": "neutral",
            "age": None,
            "personality": ["温柔", "表面随和骨子倔"],
            "system_prompt": self.system_prompt,
            "voice": "zh-CN-XiaoxiaoNeural",
            "avatar": None,
            "tags": ["tag-" + self.character_id],
            "tier": self.tier,
            "product": "angel_heart",
            "backstory": "BG-" + self.character_id,
            "awaken_quote": "AQ-" + self.character_id,
            "led_config": None,
            "awaken_sound": "",
            "voice_type": "502001",
            "background": "BG-" + self.character_id,
            "speed": -0.5,
            "transition_line": "……好像睡了很久。",
            "factory_sliders": {"温度": 70},
        }


class _Registry:
    """只实现闸门用到的 owns()。"""

    def __init__(self, owned_map):
        # owned_map: {character_id: owner_uid}
        self._m = owned_map

    def owns(self, uid, character_id):
        return self._m.get(character_id) == uid and uid is not None


@pytest.fixture
def profiles():
    return [
        _Profile("chuxue-001", TIER_PRESET, "初雪"),   # preset：人人可见
        _Profile("yaoye-004", TIER_OWNED, "曜夜"),     # owned：user-1 持有
        _Profile("mojito-005", TIER_OWNED, "莫吉托"),  # owned：无人持有 → 锁定
    ]


@pytest.fixture
def registry():
    return _Registry({"yaoye-004": "user-1"})


# ───────────────────────── ① _visible_to ─────────────────────────

def test_visible_to_preset_always_visible(profiles, registry):
    from fuguoai_core.api.character_routes import _visible_to

    p = profiles[0]  # preset
    assert _visible_to("user-1", p, registry) is True
    assert _visible_to("user-2", p, registry) is True
    assert _visible_to(None, p, registry) is True   # preset 不要求 uid


def test_visible_to_owned_requires_card(profiles, registry):
    from fuguoai_core.api.character_routes import _visible_to

    owned_mine = profiles[1]      # user-1 持有
    owned_other = profiles[2]     # 无人持有
    assert _visible_to("user-1", owned_mine, registry) is True
    assert _visible_to("user-2", owned_mine, registry) is False
    assert _visible_to("user-1", owned_other, registry) is False


def test_visible_to_none_profile_is_false(registry):
    from fuguoai_core.api.character_routes import _visible_to

    assert _visible_to("user-1", None, registry) is False


# ───────────────────────── ② handle_list_characters ─────────────────────────

@pytest.fixture
def patch_list(monkeypatch, profiles, registry):
    import fuguoai_core.api.character_routes as cr

    async def fake_auth(request):
        return "user-1", None

    class _Profiles:
        # 34176ed 起 list_all 带 product 过滤参数；本组 fixture 角色均按
        # angel_heart 处理（测试预期 count==3 全可见），故接受参数即可。
        def list_all(self, product=None):
            return profiles

        def get(self, cid):
            for p in profiles:
                if p.character_id == cid:
                    return p
            return None

    router = type("R", (), {"profiles": _Profiles()})()

    monkeypatch.setattr(cr, "_auth", fake_auth)
    monkeypatch.setattr(cr, "get_character_router", lambda: router)
    monkeypatch.setattr(cr, "get_card_registry", lambda: registry)
    monkeypatch.setattr(cr, "_apply_user_overrides", lambda uid, d: None)
    return cr


def _data(resp):
    return json.loads(resp.body.decode("utf-8"))["data"]


class _ReqQ:
    """最小 request：34176ed 起 handle_list_characters 读 query["product"]。"""
    query = {}


@pytest.mark.asyncio
async def test_locked_owned_visible_but_no_system_prompt(patch_list):
    """核心回归锁：owned 未持卡 → 可见但锁定，契约 §2.1 禁发字段绝不下发。"""
    cr = patch_list
    resp = await cr.handle_list_characters(_ReqQ())   # _add_cors 不用 request
    items = {i["character_id"]: i for i in _data(resp)["characters"]}

    assert _data(resp)["count"] == 3   # 三个都可见（不再"消失"）

    locked = items["mojito-005"]
    assert locked["locked"] is True
    assert locked["owned"] is False
    # 白名单构造：禁发字段一个都不能出现（2026-10-16 生产 E2E 抓到
    # 旧实现只脱敏 system_prompt、四字段仍泄露的真 bug 后补的回归锁）
    for f in _FORBIDDEN_LOCKED_FIELDS:
        assert f not in locked, f"locked 角色泄露禁发字段: {f}"
    # 正向：展示字段 + 分层标志在位（前端 locked 卡渲染与 is_locked 判定依赖）
    assert set(locked) == _EXPECTED_LOCKED_KEYS


@pytest.mark.asyncio
async def test_owned_held_unlocked_with_system_prompt(patch_list):
    cr = patch_list
    resp = await cr.handle_list_characters(_ReqQ())
    items = {i["character_id"]: i for i in _data(resp)["characters"]}

    mine = items["yaoye-004"]
    assert mine["owned"] is True
    assert mine["locked"] is False
    assert mine["system_prompt"] == "SP-yaoye-004"   # 持有者拿到完整人格


@pytest.mark.asyncio
async def test_preset_never_locked(patch_list):
    cr = patch_list
    resp = await cr.handle_list_characters(_ReqQ())
    items = {i["character_id"]: i for i in _data(resp)["characters"]}

    preset = items["chuxue-001"]
    assert preset["locked"] is False
    assert preset["owned"] is False
    assert preset["system_prompt"] == "SP-chuxue-001"


# ───────────────────────── ③ handle_get_character ─────────────────────────

@pytest.mark.asyncio
async def test_detail_blocks_non_holder(patch_list, monkeypatch):
    """闸门不止在目录：详情也对未持卡 uid 一律 404。"""
    cr = patch_list

    class _Req:
        match_info = {"character_id": "mojito-005"}

    resp = await cr.handle_get_character(_Req())
    assert resp.status == 404
    assert json.loads(resp.body.decode("utf-8"))["message"] == "未知角色"


@pytest.mark.asyncio
async def test_detail_allows_holder(patch_list):
    cr = patch_list

    class _Req:
        match_info = {"character_id": "yaoye-004"}

    resp = await cr.handle_get_character(_Req())
    assert resp.status == 200
    assert _data(resp)["name"] == "曜夜"
