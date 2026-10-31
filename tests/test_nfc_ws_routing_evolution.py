# -*- coding: utf-8 -*-
"""NFC 设备 WS 路由链路「自进化」测试（纯单元，不依赖运行服务器）。

与 test_nfc_tap_evolution.py 的分工：那组测 handler 单件，本组锁**真实分发链**——

  connection.ConnectionHandler._route_message（生产入口：绑定闸门+用量计量）
    → textHandle.handleTextMessage（生产全局单例 message_processor）
      → TextMessageProcessor.process_message（JSON 解析 + 注册表按 type 分发）
        → NfcTap/NfcClaimTextMessageHandler → conn.safe_send 原路回包

回归面：若哪天有人把 nfc_tap 从注册表摘掉、改错 type 字符串、或让 _route_message
不再透传文本消息，这里的测试会红——而 handler 单件测试不会。

另锁 P0-3 畸形消息防护（未知 type / 非 JSON / 连接关闭早退）不被 NFC 改动破坏。

环境说明：connection.py 传递依赖 pydub，pydub 在 py3.13 上 import pyaudioop；
本测试环境用 audioop-lts 兜底（见下方 bootstrap），与生产 requirements 等价。
"""

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest


def _import_connection():
    """导入 connection 模块；py3.13 缺 pyaudioop 时用 audioop 别名兜底。"""
    try:
        import pyaudioop  # noqa: F401
    except ModuleNotFoundError:
        try:
            import audioop as _audioop
            sys.modules["pyaudioop"] = _audioop
        except ModuleNotFoundError:
            pass  # 让后续 import 自然报错，由调用方 skip
    import fuguoai_core.connection as connection_mod
    return connection_mod


# ───────────────────────── 假件 ─────────────────────────

class _Log:
    def info(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


class _Logger:
    def bind(self, tag=None, **k):
        return _Log()


class _Conn:
    """最小 ConnectionHandler 假件：只实现文本路由链路用到的面。"""

    def __init__(self, device_id="BOX1"):
        self.device_id = device_id
        self.sent = []
        self.malformed = []
        self.logger = _Logger()

    async def safe_send(self, data):
        self.sent.append(data)

    async def note_malformed_message(self, kind, message):
        self.malformed.append((kind, message))
        return False


class _ClosingConn(_Conn):
    """note_malformed_message 返回 True：模拟超阈值主动关闭连接。"""

    async def note_malformed_message(self, kind, message):
        self.malformed.append((kind, message))
        return True


class _RouteConn(_Conn):
    """_route_message 专用：补上绑定闸门 / 用量计量 / 会话上下文字段。"""

    def __init__(self, device_id="BOX1"):
        super().__init__(device_id)
        self.session_id = "sess-1"
        self._first_msg_ts = 0.0
        self.bind_completed_event = asyncio.Event()
        self.need_bind = False
        self._usage_turns = 0
        self._usage_chars = 0


class _BindingHolder:
    """DeviceBindingStore 的可变桩：holder() 返回自身，rows 可随时改。"""

    def __init__(self, rows):
        self.rows = rows

    def __call__(self):
        return self

    def list_all(self):
        return self.rows


# ───────────────────────── 夹具 ─────────────────────────

@pytest.fixture
def ws_env(monkeypatch):
    """打桩：设备绑定表 + _process_nfc_tap / _process_nfc_claim。"""
    import fuguoai_core.api.character_routes as cr

    async def fake_tap(uid, card_uid, device_id=None, source=None):
        if card_uid == "CARD1":
            return 200, {"character_id": "char-1", "name": "初雪", "channel": "device",
                         "device_should_speak": True, "owned": True, "unclaimed": False}
        return 404, "unknown_card"

    async def fake_claim(uid, card_uid):
        if card_uid == "CARD_A":
            return 200, {"ok": True, "card_uid": "CARD_A", "owner_uid": uid}
        return 409, "already_claimed"

    monkeypatch.setattr(cr, "_process_nfc_tap", fake_tap)
    monkeypatch.setattr(cr, "_process_nfc_claim", fake_claim)
    holder = _BindingHolder([{"device_id": "BOX1", "user_id": "user-1"}])
    monkeypatch.setattr("fuguoai_core.device_access.DeviceBindingStore", holder)
    return holder


# ───────────────────────── handleTextMessage 级链路 ─────────────────────────

@pytest.mark.asyncio
async def test_route_nfc_tap_end_to_end(ws_env):
    """整条 JSON 往返：WS 文本帧 → 注册表 → handler → ack 原路回包。"""
    from fuguoai_core.handle.textHandle import handleTextMessage

    conn = _Conn("BOX1")
    await handleTextMessage(conn, json.dumps({"type": "nfc_tap", "card_uid": "CARD1"}))
    assert len(conn.sent) == 1
    # ack 形状锁定：type + ok + payload 全量合并，一个键不多一个键不少
    assert json.loads(conn.sent[0]) == {
        "type": "nfc_tap", "ok": True, "character_id": "char-1", "name": "初雪",
        "channel": "device", "device_should_speak": True, "owned": True,
        "unclaimed": False,
    }


@pytest.mark.asyncio
async def test_route_nfc_tap_unknown_card(ws_env):
    from fuguoai_core.handle.textHandle import handleTextMessage

    conn = _Conn("BOX1")
    await handleTextMessage(conn, json.dumps({"type": "nfc_tap", "card_uid": "NOPE"}))
    assert json.loads(conn.sent[0]) == {"type": "nfc_tap", "ok": False,
                                        "reason": "unknown_card"}


@pytest.mark.asyncio
async def test_route_nfc_claim_end_to_end(ws_env):
    from fuguoai_core.handle.textHandle import handleTextMessage

    conn = _Conn("BOX1")
    await handleTextMessage(conn, json.dumps({"type": "nfc_claim", "card_uid": "CARD_A"}))
    assert json.loads(conn.sent[0]) == {
        "type": "nfc_claim", "ok": True, "card_uid": "CARD_A", "owner_uid": "user-1",
    }


@pytest.mark.asyncio
async def test_route_nfc_claim_already_claimed(ws_env):
    from fuguoai_core.handle.textHandle import handleTextMessage

    conn = _Conn("BOX1")
    await handleTextMessage(conn, json.dumps({"type": "nfc_claim", "card_uid": "CARD_B"}))
    assert json.loads(conn.sent[0]) == {"type": "nfc_claim", "ok": False,
                                        "reason": "already_claimed"}


@pytest.mark.asyncio
async def test_route_nfc_tap_device_not_bound(ws_env):
    """绑定表为空 → 反查不到 uid → device_not_bound（绝不静默放行）。"""
    from fuguoai_core.handle.textHandle import handleTextMessage

    ws_env.rows = []
    conn = _Conn("BOX-UNBOUND")
    await handleTextMessage(conn, json.dumps({"type": "nfc_tap", "card_uid": "CARD1"}))
    assert json.loads(conn.sent[0]) == {"type": "nfc_tap", "ok": False,
                                        "reason": "device_not_bound"}


@pytest.mark.asyncio
async def test_route_nfc_claim_device_not_bound(ws_env):
    from fuguoai_core.handle.textHandle import handleTextMessage

    ws_env.rows = []
    conn = _Conn("BOX-UNBOUND")
    await handleTextMessage(conn, json.dumps({"type": "nfc_claim", "card_uid": "CARD_A"}))
    assert json.loads(conn.sent[0]) == {"type": "nfc_claim", "ok": False,
                                        "reason": "device_not_bound"}


# ───────────────────────── P0-3 畸形消息防护（不被 NFC 改动破坏） ─────────────────────────

@pytest.mark.asyncio
async def test_route_unknown_type_counts_malformed_and_stays_silent():
    from fuguoai_core.handle.textHandle import handleTextMessage

    conn = _Conn("BOX1")
    raw = json.dumps({"type": "no_such_type"})
    await handleTextMessage(conn, raw)
    assert conn.malformed == [("unknown_type", raw)]
    assert conn.sent == []


@pytest.mark.asyncio
async def test_route_unknown_type_closing_returns_early():
    """note_malformed_message 返回 True（超阈值关闭）→ 直接 return，不再路由。"""
    from fuguoai_core.handle.textHandle import handleTextMessage

    conn = _ClosingConn("BOX1")
    await handleTextMessage(conn, json.dumps({"type": "no_such_type"}))
    assert len(conn.malformed) == 1
    assert conn.sent == []


@pytest.mark.asyncio
async def test_route_non_json_echoes_after_count():
    from fuguoai_core.handle.textHandle import handleTextMessage

    conn = _Conn("BOX1")
    await handleTextMessage(conn, "hello")
    assert conn.malformed[0][0] == "non_json"
    assert conn.sent == ["hello"]


@pytest.mark.asyncio
async def test_route_numeric_message_echoed():
    from fuguoai_core.handle.textHandle import handleTextMessage

    conn = _Conn("BOX1")
    await handleTextMessage(conn, "42")
    assert conn.malformed == []
    assert conn.sent == ["42"]


# ───────────────────────── 生产单例 + _route_message 入口 ─────────────────────────

def test_production_singleton_registers_nfc_handlers():
    """锁生产实际使用的全局单例（不是新建的 registry）。"""
    from fuguoai_core.handle.textHandle import message_processor

    types = message_processor.registry.get_supported_types()
    assert "nfc_tap" in types
    assert "nfc_claim" in types


@pytest.mark.asyncio
async def test_connection_route_message_nfc_tap_end_to_end(ws_env, monkeypatch):
    """锁生产入口 ConnectionHandler._route_message：绑定闸门 + 用量计量 + 分发。"""
    connection_mod = _import_connection()
    if connection_mod is None:  # pragma: no cover
        pytest.skip("connection 模块不可导入（缺重依赖）")

    monkeypatch.setattr(connection_mod, "METRICS",
                        SimpleNamespace(inc=lambda *a, **k: None))
    monkeypatch.setattr(connection_mod, "get_usage_store",
                        lambda: SimpleNamespace(record_turn=lambda *a, **k: None))

    conn = _RouteConn("BOX1")
    conn.bind_completed_event.set()
    await connection_mod.ConnectionHandler._route_message(
        conn, json.dumps({"type": "nfc_tap", "card_uid": "CARD1"}))
    # 用量计量照记（NFC 消息也是设备上行）
    assert conn._usage_turns == 1
    assert conn._usage_chars > 0
    ack = json.loads(conn.sent[0])
    assert ack["ok"] is True and ack["character_id"] == "char-1"
