# -*- coding: utf-8 -*-
"""天使心 NFC 后端「自进化」测试（纯单元，不依赖运行服务器）。

覆盖 2026-10-13 新增的设备侧 WS 碰卡/领卡链路：
  ① _process_nfc_tap 共享核心（HTTP 与 WS 共用）
     - 缺 card_uid → 400
     - 未知卡 → 404 unknown_card（后端硬拒，前端/设备无从伪造）
     - 已知卡 → 200，owned / unclaimed 正确；device 面注入 system_prompt
  ② _process_nfc_claim 共享核心（HTTP 与 WS 共用）
     - 缺 card_uid → 400
     - 未知卡 → 404 unknown_card；已领 → 409 already_claimed
  ③ 设备 WS NfcTapTextMessageHandler：经 device_id 反查 uid → 调核心 → 原路回包
     - 未绑定设备 → device_not_bound
  ④ 设备 WS NfcClaimTextMessageHandler：同上，领卡回包
  ⑤ 注册表已登记 nfc_tap / nfc_claim 两个消息类型
"""

import json

import pytest

from fuguoai_core.utils.character_store import CHANNEL_DEVICE


# ───────────────────────── 共享核心测试夹具 ─────────────────────────

class _Card:
    def __init__(self, character_id=None, owner_uid=None):
        self._d = {"character_id": character_id, "owner_uid": owner_uid}

    def get(self, k, default=None):
        return self._d.get(k, default)


class _Registry:
    def __init__(self, cards):
        self._cards = cards

    def get(self, uid):
        return self._cards.get(uid)

    def owns(self, uid, cid):
        for c in self._cards.values():
            if c.get("character_id") == cid and c.get("owner_uid") == uid:
                return True
        return False


class _Profile:
    def __init__(self, cid, name, system_prompt="SP"):
        self.character_id = cid
        self.name = name
        self.system_prompt = system_prompt


class _Router:
    def __init__(self, profiles):
        self.profiles = profiles

    def validate_id(self, cid):
        return cid in self.profiles

    def on_nfc_tap(self, uid, cid, device_id=None, source=CHANNEL_DEVICE, now=0.0):
        if not self.validate_id(cid):
            return {"ok": False, "reason": "unknown_character"}
        p = self.profiles[cid]
        return {
            "ok": True,
            "character_id": cid,
            "name": p.name,
            "channel": source,
            "device_should_speak": source == CHANNEL_DEVICE,
        }


@pytest.fixture
def patch_nfc_core(monkeypatch):
    """在 character_routes 模块上打桩：卡片表 + 角色路由 + 注入捕获。"""
    import fuguoai_core.api.character_routes as cr

    cards = {
        "CARD_OWNED": _Card("char-1", "user-1"),
        "CARD_UNCLAIMED": _Card("char-2", None),
    }
    registry = _Registry(cards)
    profiles = {
        "char-1": _Profile("char-1", "初雪"),
        "char-2": _Profile("char-2", "零"),
    }
    router = _Router(profiles)
    injected = {}

    monkeypatch.setattr(cr, "get_card_registry", lambda: registry)
    monkeypatch.setattr(cr, "get_character_router", lambda: router)
    monkeypatch.setattr(
        cr,
        "_inject_character_into_device",
        lambda did, cid: injected.update({"did": did, "cid": cid}) or True,
    )
    return injected


# ───────────────────────── ① _process_nfc_tap ─────────────────────────

@pytest.mark.asyncio
async def test_process_nfc_tap_missing_card_uid(patch_nfc_core):
    from fuguoai_core.api.character_routes import _process_nfc_tap

    status, payload = await _process_nfc_tap("user-1", "")
    assert status == 400 and payload == "card_uid 必填"


@pytest.mark.asyncio
async def test_process_nfc_tap_unknown_card(patch_nfc_core):
    from fuguoai_core.api.character_routes import _process_nfc_tap

    status, payload = await _process_nfc_tap("user-1", "NOPE")
    assert status == 404 and payload == "unknown_card"


@pytest.mark.asyncio
async def test_process_nfc_tap_known_owned_and_injects(patch_nfc_core):
    from fuguoai_core.api.character_routes import _process_nfc_tap

    status, payload = await _process_nfc_tap(
        "user-1", "CARD_OWNED", device_id="box-1", source=CHANNEL_DEVICE
    )
    assert status == 200
    assert payload["character_id"] == "char-1"
    assert payload["name"] == "初雪"
    assert payload["owned"] is True          # owner == 当前 uid
    assert payload["unclaimed"] is False      # 已领
    assert payload["device_should_speak"] is True
    # device 面：system_prompt 注入活连接
    assert patch_nfc_core == {"did": "box-1", "cid": "char-1"}


@pytest.mark.asyncio
async def test_process_nfc_tap_known_unclaimed(patch_nfc_core):
    from fuguoai_core.api.character_routes import _process_nfc_tap

    status, payload = await _process_nfc_tap(
        "user-2", "CARD_UNCLAIMED", device_id="box-1", source=CHANNEL_DEVICE
    )
    assert status == 200
    assert payload["owned"] is False
    assert payload["unclaimed"] is True       # 未领 → 客户端应去 claim


# ───────────────────────── ② _process_nfc_claim ─────────────────────────

class _ClaimRegistry:
    """简化的 card_registry.claim 桩。"""

    def __init__(self, state):
        # state: {card_uid: owner_uid or None}
        self._state = dict(state)

    def claim(self, card_uid, uid):
        if card_uid not in self._state:
            return {"ok": False, "reason": "unknown_card"}
        if self._state[card_uid] is not None:
            return {"ok": False, "reason": "already_claimed"}
        self._state[card_uid] = uid
        return {"ok": True, "card_uid": card_uid, "owner_uid": uid}


@pytest.fixture
def patch_claim_core(monkeypatch):
    import fuguoai_core.api.character_routes as cr

    reg = _ClaimRegistry({"CARD_A": None, "CARD_CLAIMED": "someone-else"})
    monkeypatch.setattr(cr, "get_card_registry", lambda: reg)
    return reg


@pytest.mark.asyncio
async def test_process_nfc_claim_missing_card_uid(patch_claim_core):
    from fuguoai_core.api.character_routes import _process_nfc_claim

    status, payload = await _process_nfc_claim("user-1", "")
    assert status == 400 and payload == "card_uid 必填"


@pytest.mark.asyncio
async def test_process_nfc_claim_unknown(patch_claim_core):
    from fuguoai_core.api.character_routes import _process_nfc_claim

    status, payload = await _process_nfc_claim("user-1", "NOPE")
    assert status == 404 and payload == "unknown_card"


@pytest.mark.asyncio
async def test_process_nfc_claim_already_claimed(patch_claim_core):
    from fuguoai_core.api.character_routes import _process_nfc_claim

    status, payload = await _process_nfc_claim("user-1", "CARD_CLAIMED")
    assert status == 409 and payload == "already_claimed"


@pytest.mark.asyncio
async def test_process_nfc_claim_happy(patch_claim_core):
    from fuguoai_core.api.character_routes import _process_nfc_claim

    status, payload = await _process_nfc_claim("user-1", "CARD_A")
    assert status == 200 and payload.get("ok") is True
    # 真的写入了 owner
    assert patch_claim_core._state["CARD_A"] == "user-1"


# ───────────────────────── ③④ 设备 WS handler ─────────────────────────

class _FakeBinding:
    def __init__(self, rows):
        self._rows = rows

    def list_all(self):
        return self._rows


class _Conn:
    def __init__(self, device_id):
        self.device_id = device_id
        self.sent = []

    async def safe_send(self, data):
        self.sent.append(data)


@pytest.mark.asyncio
async def test_ws_nfc_tap_happy(monkeypatch):
    import fuguoai_core.api.character_routes as cr

    async def fake_tap(uid, card_uid, device_id=None, source=CHANNEL_DEVICE):
        if card_uid == "CARD1":
            return (200, {"character_id": "char-1", "name": "初雪", "channel": "device",
                          "device_should_speak": True, "owned": True, "unclaimed": False})
        return (404, "unknown_card")

    monkeypatch.setattr(cr, "_process_nfc_tap", fake_tap)
    monkeypatch.setattr(
        "fuguoai_core.device_access.DeviceBindingStore",
        lambda: _FakeBinding([{"device_id": "BOX1", "user_id": "user-1"}]),
    )

    from fuguoai_core.handle.textHandler.nfcTapMessageHandler import (
        NfcTapTextMessageHandler,
    )

    conn = _Conn("BOX1")
    await NfcTapTextMessageHandler().handle(conn, {"type": "nfc_tap", "card_uid": "CARD1"})
    assert len(conn.sent) == 1
    ack = json.loads(conn.sent[0])
    assert ack["ok"] is True and ack["character_id"] == "char-1"


@pytest.mark.asyncio
async def test_ws_nfc_tap_device_not_bound(monkeypatch):
    import fuguoai_core.api.character_routes as cr

    async def fake_tap(uid, card_uid, device_id=None, source=CHANNEL_DEVICE):
        return (200, {})

    monkeypatch.setattr(cr, "_process_nfc_tap", fake_tap)
    # 绑定表为空 → 反查不到 uid
    monkeypatch.setattr(
        "fuguoai_core.device_access.DeviceBindingStore",
        lambda: _FakeBinding([]),
    )

    from fuguoai_core.handle.textHandler.nfcTapMessageHandler import (
        NfcTapTextMessageHandler,
    )

    conn = _Conn("BOX-UNBOUND")
    await NfcTapTextMessageHandler().handle(conn, {"type": "nfc_tap", "card_uid": "CARD1"})
    ack = json.loads(conn.sent[0])
    assert ack["ok"] is False and ack["reason"] == "device_not_bound"


@pytest.mark.asyncio
async def test_ws_nfc_claim_happy(monkeypatch):
    import fuguoai_core.api.character_routes as cr

    async def fake_claim(uid, card_uid):
        if card_uid == "CARD_A":
            return (200, {"ok": True, "card_uid": "CARD_A", "owner_uid": uid})
        return (409, "already_claimed")

    monkeypatch.setattr(cr, "_process_nfc_claim", fake_claim)
    monkeypatch.setattr(
        "fuguoai_core.device_access.DeviceBindingStore",
        lambda: _FakeBinding([{"device_id": "BOX1", "user_id": "user-1"}]),
    )

    from fuguoai_core.handle.textHandler.nfcClaimMessageHandler import (
        NfcClaimTextMessageHandler,
    )

    conn = _Conn("BOX1")
    await NfcClaimTextMessageHandler().handle(conn, {"type": "nfc_claim", "card_uid": "CARD_A"})
    ack = json.loads(conn.sent[0])
    assert ack["ok"] is True and ack["owner_uid"] == "user-1"


# ───────────────────────── ⑤ 注册表登记 ─────────────────────────

def test_registry_registers_nfc_types():
    from fuguoai_core.handle.textMessageHandlerRegistry import (
        TextMessageHandlerRegistry,
    )

    reg = TextMessageHandlerRegistry()
    types = reg.get_supported_types()
    assert "nfc_tap" in types
    assert "nfc_claim" in types
