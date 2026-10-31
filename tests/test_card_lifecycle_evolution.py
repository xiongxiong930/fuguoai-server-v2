# -*- coding: utf-8 -*-
"""天使心「卡片生命周期」自进化测试（纯单元 + 薄端点，不依赖运行服务器）。

对应自进化方向 A1：card_registry 与 wipe/transfer/collect/lineage/cards_me
此前**零测试覆盖**，而这些正是防克隆与传承的硬规则所在。

覆盖：
  ① _norm_uid 归一化（不同读卡端形态不一致 → 不归一会把同一张卡判成 unknown_card）
  ② CardRegistry 存储层：get/resolve_character/owns/owned_character_ids/
     list_by_uid/get_by_serial
  ③ claim：未知卡 / 缺 uid / 已领拒绝 / 成功绑定并写入 lineage
  ④ mark_wiped：非持有者 → unknown_card（**不泄露卡片存在**）
  ⑤ release：未 wipe 先转 → must_wipe_before_transfer；wipe 后转 → 释放 owner
     且 lineage 保留（这就是"有履历"）
  ⑥ set_collected：仅持有者可改
  ⑦ 端点层（薄）：cards_me **不得回传 card_uid**（防克隆关键）、
     transfer 未 wipe → 409 / 未知编号 → 404、lineage uid 脱敏
"""

import json

import pytest

from fuguoai_core.utils.card_registry import CardRegistry, _norm_uid


# ───────────────────────── 夹具 ─────────────────────────

def _seed(path, cards: dict) -> None:
    """直接写注册表文件（键用未归一化形态，顺带验证读取端归一化）。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"cards": cards}, f, ensure_ascii=False)


@pytest.fixture
def registry(tmp_path):
    path = tmp_path / "card_registry.json"
    _seed(path, {
        # 形态故意带分隔符/小写，读端必须归一化才能命中
        "53:44-c0fa": {
            "character_id": "chuxue-001",
            "serial": "AH-001",
            "owner_uid": "user-1",
            "claimed_at": 1000.0,
            "created_at": 900.0,
            "lineage": ["user-1"],
            "status": "active",
        },
        "ABCDEF": {
            "character_id": "mojito-005",
            "serial": "AH-002",
            "owner_uid": None,      # 未领
            "created_at": 910.0,
            "lineage": [],
            "status": "active",
        },
    })
    return CardRegistry(path=str(path))


class _Req:
    """极简 request 桩：端点只用到 match_info / json()。"""

    def __init__(self, **match_info):
        self.match_info = match_info

    async def json(self):
        return {}


def _payload(resp):
    """从 aiohttp json_response 里取回 data 段。"""
    return json.loads(resp.body.decode("utf-8"))


# ───────────────────────── ① 归一化 ─────────────────────────

def test_norm_uid_strips_separators_and_uppercases():
    assert _norm_uid(" 53:44-c0_fa ") == "5344C0FA"
    assert _norm_uid("ab:cd") == "ABCD"
    assert _norm_uid("") == ""


def test_registry_get_normalizes_key(registry):
    # 用与登记时不同形态（小写 + 分隔符）查，必须命中同一张物理卡
    assert registry.get("5344c0fa") is not None
    assert registry.get("53:44-C0-FA")["serial"] == "AH-001"


def test_resolve_character_unregistered_is_none(registry):
    assert registry.resolve_character("5344C0FA") == "chuxue-001"
    assert registry.resolve_character("NOPE") is None


# ───────────────────────── ② 查询 ─────────────────────────

def test_owns_and_owned_character_ids(registry):
    assert registry.owns("user-1", "chuxue-001") is True
    assert registry.owns("user-2", "chuxue-001") is False
    assert registry.owns("user-1", "mojito-005") is False   # 未领的卡谁都不持有
    assert registry.owned_character_ids("user-1") == {"chuxue-001"}
    assert registry.owned_character_ids("user-2") == set()


def test_list_by_uid_only_owner(registry):
    mine = registry.list_by_uid("user-1")
    assert len(mine) == 1
    assert mine[0]["serial"] == "AH-001"
    # 内部视图带 card_uid（端点层负责剔除）
    assert mine[0]["card_uid"] == "5344C0FA"
    assert registry.list_by_uid("user-2") == []


def test_get_by_serial(registry):
    cuid, card = registry.get_by_serial("AH-001")
    assert cuid == "5344C0FA" and card["character_id"] == "chuxue-001"
    assert registry.get_by_serial("NOPE") == (None, None)
    assert registry.get_by_serial("") == (None, None)


# ───────────────────────── ③ claim ─────────────────────────

def test_claim_unknown_card(registry):
    ack = registry.claim("NOPE", "user-9")
    assert ack == {"ok": False, "reason": "unknown_card"}


def test_claim_requires_uid(registry):
    assert registry.claim("ABCDEF", "") == {"ok": False, "reason": "uid_required"}


def test_claim_already_claimed_rejected(registry):
    # 捡到别人已领的卡，占不走
    assert registry.claim("5344C0FA", "user-2")["reason"] == "already_claimed"


def test_claim_happy_binds_owner_and_lineage(registry):
    ack = registry.claim("ABCDEF", "user-7")
    assert ack["ok"] is True and ack["serial"] == "AH-002"
    assert registry.owns("user-7", "mojito-005") is True
    assert "user-7" in registry.get("ABCDEF")["lineage"]
    # 已领后再领 → 拒绝
    assert registry.claim("ABCDEF", "user-8")["reason"] == "already_claimed"


# ───────────────────────── ④ mark_wiped ─────────────────────────

def test_mark_wiped_non_owner_not_leaked(registry):
    # 不是自己的卡：统一 unknown_card，不泄露"这张卡存在"
    assert registry.mark_wiped("5344C0FA", "user-2")["reason"] == "unknown_card"


def test_mark_wiped_happy(registry):
    ack = registry.mark_wiped("5344C0FA", "user-1")
    assert ack["ok"] is True
    assert registry.get("5344C0FA")["wiped_at"] > 0


# ───────────────────────── ⑤ release（二手转手） ─────────────────────────

def test_release_without_wipe_rejected(registry):
    # 硬约束：绝不把前主人隐私留给下一任
    ack = registry.release("5344C0FA", "user-1")
    assert ack == {"ok": False, "reason": "must_wipe_before_transfer"}


def test_release_after_wipe_frees_owner_keeps_lineage(registry):
    assert registry.mark_wiped("5344C0FA", "user-1")["ok"] is True
    ack = registry.release("5344C0FA", "user-1")
    assert ack["ok"] is True and ack["lineage_count"] >= 1
    c = registry.get("5344C0FA")
    assert c["owner_uid"] is None          # 已释放，等下一任 claim
    assert "user-1" in c["lineage"]        # 履历保留 → "有履历"
    assert c["status"] == "active"
    # 下一任可以领
    assert registry.claim("5344C0FA", "user-3")["ok"] is True


def test_release_non_owner(registry):
    assert registry.release("5344C0FA", "user-2")["reason"] == "unknown_card"


# ───────────────────────── ⑥ set_collected ─────────────────────────

def test_set_collected_owner_only(registry):
    assert registry.set_collected("5344C0FA", "user-2")["reason"] == "unknown_card"
    ack = registry.set_collected("5344C0FA", "user-1", True)
    assert ack["ok"] is True and ack["collected"] is True
    assert registry.get("5344C0FA")["collected"] is True
    assert registry.set_collected("5344C0FA", "user-1", False)["collected"] is False


# ───────────────────────── ⑦ 端点层 ─────────────────────────

@pytest.fixture
def patch_endpoints(monkeypatch, registry):
    import fuguoai_core.api.character_routes as cr

    async def fake_auth(request):
        return "user-1", None

    class _P:
        def __init__(self, cid, name):
            self.character_id = cid
            self.name = name

    class _Profiles:
        def __init__(self):
            self._m = {"chuxue-001": _P("chuxue-001", "初雪"),
                       "mojito-005": _P("mojito-005", "莫吉托")}

        def get(self, cid):
            return self._m.get(cid)

        def list_all(self):
            return list(self._m.values())

    class _Sessions:
        def get(self, uid):
            return None

        def clear(self, uid):
            return None

    router = type("R", (), {"profiles": _Profiles(), "sessions": _Sessions()})()

    monkeypatch.setattr(cr, "_auth", fake_auth)
    monkeypatch.setattr(cr, "get_card_registry", lambda: registry)
    monkeypatch.setattr(cr, "get_character_router", lambda: router)
    monkeypatch.setattr(cr, "_apply_user_overrides", lambda uid, d: None)
    return cr


@pytest.mark.asyncio
async def test_cards_me_never_leaks_card_uid(patch_endpoints):
    """card_uid 是防克隆的关键，只能由 NFC 硬件读出上报，绝不经 API 泄露。"""
    cr = patch_endpoints
    resp = await cr.handle_cards_me(_Req())
    cards = _payload(resp)["data"]["cards"]
    assert len(cards) == 1
    item = cards[0]
    assert "card_uid" not in item          # 硬断言：绝不回传
    assert item["serial"] == "AH-001"      # 对外一律用 human 编号寻址
    assert item["character_name"] == "初雪"


@pytest.mark.asyncio
async def test_transfer_without_wipe_returns_409(patch_endpoints):
    cr = patch_endpoints
    resp = await cr.handle_card_transfer(_Req(serial="AH-001"))
    assert resp.status == 409
    assert _payload(resp)["message"] == "must_wipe_before_transfer"


@pytest.mark.asyncio
async def test_transfer_unknown_serial_404(patch_endpoints):
    cr = patch_endpoints
    resp = await cr.handle_card_transfer(_Req(serial="NOPE"))
    assert resp.status == 404
    assert _payload(resp)["message"] == "unknown_card"


@pytest.mark.asyncio
async def test_lineage_masks_previous_owners(patch_endpoints):
    cr = patch_endpoints
    resp = await cr.handle_character_lineage(_Req(character_id="chuxue-001"))
    data = _payload(resp)["data"]
    assert data["serial"] == "AH-001"
    assert data["generation"] == 1
    # 历任持有者脱敏，不暴露完整 uid
    assert all("***" in m for m in data["lineage"])


@pytest.mark.asyncio
async def test_lineage_non_owner_404(patch_endpoints):
    cr = patch_endpoints
    resp = await cr.handle_character_lineage(_Req(character_id="mojito-005"))
    assert resp.status == 404


def test_mask_uid():
    from fuguoai_core.api.character_routes import _mask_uid

    assert _mask_uid("abcdefg") == "abc***fg"
    assert _mask_uid("abc") == "***"     # 过短一律打码
    assert _mask_uid("") == "***"
