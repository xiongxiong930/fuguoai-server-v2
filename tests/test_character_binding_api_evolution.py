# -*- coding: utf-8 -*-
"""契约 2.4 `/character-binding` 接口自进化测试（2026-10-16）。

锁四类语义：
① PUT 新建：未登记 UID → 建映射（serial 缺省自动 MOCK_<尾6位>，owner 空等 claim）
② PUT 换绑：自己的卡 → 换 character_id，**不动 owner/lineage/claimed_at**
③ 闸门：未知 character_id 400（绝不接受客户端自报角色）；缺字段 400
④ GET：命中回 binding / 未登记 404；UID 归一化（大小写/分隔符不影响）

**权限收紧（2026-10-16，防三洞）**：
⑤ owned-tier 角色仅运营白名单可绑（缺省空=全关）——防 mock UID 免费解锁锁定角色
⑥ 已被他人 claim 的 UID 仅本人/白名单可改——防劫持他人卡映射
⑦ serial 仅白名单可指定；GET 对非 owner/非白名单脱敏（只见公开字段）
另锁一条集成语义：claim 之后 bind 换角色不丢 owner（归属与映射两条线）。
"""

import pytest

from fuguoai_core.api import character_routes as cr
from fuguoai_core.utils.card_registry import CardRegistry
from fuguoai_core.utils.character_store import CHANNEL_APP, TIER_PRESET, TIER_OWNED


class _Req:
    def __init__(self, body=None, query=None, **match_info):
        self.match_info = match_info
        self._body = body or {}
        self.query = query or {}

    async def json(self):
        return self._body


def _payload(resp):
    import json
    return json.loads(resp.body.decode("utf-8"))


class _P:
    """最小 profile 桩：只要 character_id/tier（_is_owned_tier 判定用）。"""

    def __init__(self, cid, tier):
        self.character_id = cid
        self.tier = tier


# 与 159 现盘一致：小满/阿澈 preset；知秋/灯灯 owned
_TIERS = {
    "xiaoman-001": TIER_PRESET,
    "ache-002": TIER_PRESET,
    "zhiqu-003": TIER_OWNED,
    "dengdeng-004": TIER_OWNED,
}


@pytest.fixture
def env(monkeypatch, tmp_path):
    reg = CardRegistry(path=str(tmp_path / "card_registry.json"))

    class _Profiles:
        def validate_id(self, cid):
            return cid in _TIERS

        def get(self, cid):
            return _P(cid, _TIERS[cid]) if cid in _TIERS else None

        def list_all(self, product=None):
            return []

    router = type("R", (), {"profiles": _Profiles()})()

    async def fake_auth(request):
        return "user-1", None

    monkeypatch.setattr(cr, "_auth", fake_auth)
    monkeypatch.setattr(cr, "get_card_registry", lambda: reg)
    monkeypatch.setattr(cr, "get_character_router", lambda: router)
    return type("E", (), {"reg": reg})()


@pytest.fixture
def admin_env(env, monkeypatch):
    """把 user-1 加进运营白名单（monkeypatch 配置读取，不碰真实 config）。"""
    monkeypatch.setattr(cr, "_binding_admin_uids", lambda: {"user-1"})
    return env


@pytest.mark.asyncio
async def test_put_creates_new_binding_with_mock_serial(env):
    resp = await cr.handle_put_binding(_Req(body={
        "nfc_uid": "aa:bb:cc:dd:ee:ff", "character_id": "ache-002"}))
    assert resp.status == 200
    b = _payload(resp)["data"]["binding"]
    # UID 归一化（去冒号大写）
    assert b["card_uid"] == "AABBCCDDEEFF"
    assert b["character_id"] == "ache-002"
    assert b["serial"].startswith("MOCK_")
    assert b["owner_uid"] is None          # 等 claim
    assert b["status"] == "registered"
    assert b["lineage"] == []


@pytest.mark.asyncio
async def test_put_rebind_keeps_owner_and_lineage(env):
    # 自己的卡换角色：owner/lineage 不丢（preset 角色，本人 claim）
    assert env.reg.bind("112233445566", "xiaoman-001", serial="TAG_XZJ_01_XIAOMAN")["ok"]
    assert env.reg.claim("112233445566", "user-1", now=1.0)["ok"]
    resp = await cr.handle_put_binding(_Req(body={
        "nfc_uid": "112233445566", "character_id": "ache-002"}))
    assert resp.status == 200
    b = _payload(resp)["data"]["binding"]
    assert b["character_id"] == "ache-002"
    assert b["owner_uid"] == "user-1"      # 归属不动（claim 是另一条线）
    assert b["lineage"] == ["user-1"]
    assert b["serial"] == "TAG_XZJ_01_XIAOMAN"  # 未传 serial 不改原编号


@pytest.mark.asyncio
async def test_put_rejects_unknown_character(env):
    resp = await cr.handle_put_binding(_Req(body={
        "nfc_uid": "AABBCCDDEEFF", "character_id": "not-exist-999"}))
    assert resp.status == 400
    assert _payload(resp)["message"] == "unknown_character"
    # 未落库
    assert env.reg.get("AABBCCDDEEFF") is None


@pytest.mark.asyncio
async def test_put_rejects_missing_fields(env):
    r1 = await cr.handle_put_binding(_Req(body={"nfc_uid": "AABB"}))
    assert r1.status == 400
    r2 = await cr.handle_put_binding(_Req(body={"character_id": "ache-002"}))
    assert r2.status == 400


@pytest.mark.asyncio
async def test_get_binding_hit_and_miss(env):
    assert (await cr.handle_put_binding(_Req(body={
        "nfc_uid": "0A1B2C3D4E5F", "character_id": "ache-002"}))).status == 200
    # GET 命中（大小写/分隔符无关）
    hit = await cr.handle_get_binding(_Req(query={"nfc_uid": "0a:1b-2c_3d 4e5f"}))
    assert hit.status == 200
    assert _payload(hit)["data"]["binding"]["character_id"] == "ache-002"
    # GET 未登记 → 404
    miss = await cr.handle_get_binding(_Req(query={"nfc_uid": "FFFFFFFFFFFF"}))
    assert miss.status == 404
    assert _payload(miss)["message"] == "unknown_card"


@pytest.mark.asyncio
async def test_get_binding_requires_uid(env):
    resp = await cr.handle_get_binding(_Req(query={}))
    assert resp.status == 400


# ───────────────── 权限收紧（2026-10-16，防三洞） ─────────────────

@pytest.mark.asyncio
async def test_put_owned_tier_requires_admin(env, monkeypatch):
    """洞①封堵：owned-tier 角色非白名单不可绑（防 mock UID 免费解锁知秋/灯灯）。"""
    resp = await cr.handle_put_binding(_Req(body={
        "nfc_uid": "AABBCCDDEEFF", "character_id": "zhiqu-003"}))
    assert resp.status == 403
    assert _payload(resp)["message"] == "admin_only"
    assert env.reg.get("AABBCCDDEEFF") is None   # 未落库
    # 白名单可绑；且 admin 不代设 owner（领卡只走 /nfc/claim）
    monkeypatch.setattr(cr, "_binding_admin_uids", lambda: {"user-1"})
    ok = await cr.handle_put_binding(_Req(body={
        "nfc_uid": "AABBCCDDEEFF", "character_id": "zhiqu-003"}))
    assert ok.status == 200
    assert _payload(ok)["data"]["binding"]["owner_uid"] is None


@pytest.mark.asyncio
async def test_put_other_claimed_card_rejected(env):
    """洞②封堵：已被他人 claim 的 UID，非本人非白名单改不了（防劫持卡映射）。"""
    assert env.reg.bind("112233445566", "xiaoman-001")["ok"]
    assert env.reg.claim("112233445566", "user-9", now=1.0)["ok"]
    resp = await cr.handle_put_binding(_Req(body={
        "nfc_uid": "112233445566", "character_id": "ache-002"}))
    assert resp.status == 403
    assert _payload(resp)["message"] == "not_card_owner"
    # 映射未被改动
    assert env.reg.get("112233445566")["character_id"] == "xiaoman-001"


@pytest.mark.asyncio
async def test_put_admin_can_rebind_other_claimed_card(env, admin_env):
    """白名单豁免：运营可改他人已 claim 卡的映射（owner/lineage 仍不动）。"""
    assert env.reg.bind("112233445566", "xiaoman-001")["ok"]
    assert env.reg.claim("112233445566", "user-9", now=1.0)["ok"]
    resp = await cr.handle_put_binding(_Req(body={
        "nfc_uid": "112233445566", "character_id": "dengdeng-004"}))
    assert resp.status == 200
    b = _payload(resp)["data"]["binding"]
    assert b["character_id"] == "dengdeng-004"
    assert b["owner_uid"] == "user-9"


@pytest.mark.asyncio
async def test_put_serial_only_honored_for_admin(env, monkeypatch):
    """洞③封堵：serial 仅白名单可指定，其余自动 MOCK_（防伪造出厂编号）。"""
    r1 = await cr.handle_put_binding(_Req(body={
        "nfc_uid": "AABBCCDDEEFF", "character_id": "ache-002", "serial": "HZX-0001"}))
    assert r1.status == 200
    assert _payload(r1)["data"]["binding"]["serial"].startswith("MOCK_")
    monkeypatch.setattr(cr, "_binding_admin_uids", lambda: {"user-1"})
    r2 = await cr.handle_put_binding(_Req(body={
        "nfc_uid": "FFEEDDCCBBAA", "character_id": "ache-002", "serial": "HZX-0002"}))
    assert r2.status == 200
    assert _payload(r2)["data"]["binding"]["serial"] == "HZX-0002"


@pytest.mark.asyncio
async def test_get_redacts_personal_fields_for_non_owner(env):
    """脱敏：非 owner/非白名单 GET 只见公开字段，owner_uid/tuning/lineage 不下发。"""
    assert env.reg.bind("112233445566", "xiaoman-001", serial="TAG_XZJ_01_XIAOMAN")["ok"]
    assert env.reg.claim("112233445566", "user-9", now=1.0)["ok"]
    # 伪造点个人数据，验证不被下发
    cards = env.reg._load()
    cards["112233445566"]["tuning"] = {"name": "私人调校"}
    cards["112233445566"]["lineage"] = ["user-9"]
    env.reg._save(cards)
    resp = await cr.handle_get_binding(_Req(query={"nfc_uid": "112233445566"}))
    assert resp.status == 200
    b = _payload(resp)["data"]["binding"]
    assert set(b.keys()) == {"card_uid", "character_id", "serial", "status"}
    assert b["character_id"] == "xiaoman-001"
    assert "owner_uid" not in b and "tuning" not in b


@pytest.mark.asyncio
async def test_get_full_binding_for_owner_and_admin(env, admin_env):
    """owner 本人与白名单 GET 见全量（含 owner_uid）。"""
    assert env.reg.bind("112233445566", "xiaoman-001")["ok"]
    assert env.reg.claim("112233445566", "user-9", now=1.0)["ok"]
    # 白名单视角：全量
    resp = await cr.handle_get_binding(_Req(query={"nfc_uid": "112233445566"}))
    assert resp.status == 200
    assert _payload(resp)["data"]["binding"]["owner_uid"] == "user-9"
    # owner 本人视角：换 auth 为 user-9
    import fuguoai_core.api.character_routes as _cr

    async def auth_as_owner(request):
        return "user-9", None

    _cr._auth, saved = auth_as_owner, _cr._auth
    try:
        resp2 = await _cr.handle_get_binding(_Req(query={"nfc_uid": "112233445566"}))
        assert resp2.status == 200
        assert _payload(resp2)["data"]["binding"]["owner_uid"] == "user-9"
    finally:
        _cr._auth = saved
