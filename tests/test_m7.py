"""M7 设备级 WS 鉴权逻辑验证（在容器内 /opt/fuguoai-server 环境下运行）。

覆盖三块：
  Part1 DeviceBindingStore.issue/verify/clear/get（含防冒充 device-id）
  Part2 handle_bind 响应返回 device_token（隔离库，不污染真实业务）
  Part3 WebSocketServer._handle_auth 的 device_ws_auth 分支（mock websocket）
"""
import asyncio
import json
import os
import sys
import types
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, "/opt/fuguoai-server")

TMP = "/tmp/m7_test"
os.makedirs(TMP, exist_ok=True)
for f in os.listdir(TMP):
    try:
        os.remove(os.path.join(TMP, f))
    except Exception:
        pass

from fuguoai_core.device_access import DeviceBindingStore
from fuguoai_core.websocket_server import WebSocketServer, AuthenticationError  # 顶层 import，避免事件循环嵌套


# ── Part1: Store 三件套 ──────────────────────────────────────────────
def test_store():
    db = os.path.join(TMP, "device_agent.db")
    b = DeviceBindingStore(db_path=db)

    tok = b.issue_device_token("AABBCC", "fixedtok123")
    assert tok == "fixedtok123", tok
    print("[P1] issue ok (row absent -> upsert insert)")

    # 真实绑定顺序：先 set_status(active) 再 issue，验证 device_token 与 status 共存
    b.set_status("AABBCC", "active", user_id="u1")
    b.issue_device_token("AABBCC", "fixedtok123")
    assert b.verify_device_token("AABBCC", "fixedtok123") is True
    print("[P1] verify ok (correct token, active)")

    assert b.verify_device_token("AABBCC", "fixedtok123") is True
    print("[P1] verify ok (correct token)")

    assert b.verify_device_token("AABBCC", "wrong") is False
    print("[P1] verify reject (wrong token)")

    # 防冒充：用他人 device-id + 真实 token
    assert b.verify_device_token("SPOOFID", "fixedtok123") is False
    print("[P1] verify reject (spoofed device-id)")

    # 未激活拒绝
    b.set_status("AABBCC", "unbound", user_id="u1")
    assert b.verify_device_token("AABBCC", "fixedtok123") is False
    print("[P1] verify reject (unbound)")
    b.set_status("AABBCC", "active", user_id="u1")

    # 清除后拒绝
    b.clear_device_token("AABBCC")
    assert b.verify_device_token("AABBCC", "fixedtok123") is False
    print("[P1] verify reject (after clear)")

    # 重新签发 + get
    b.issue_device_token("AABBCC", "tok2")
    assert b.get_device_token("AABBCC") == "tok2"
    print("[P1] get ok")
    print("PART1_OK")


# ── Part2: handle_bind 返回 device_token（隔离库）─────────────────────
async def test_handle_bind():
    import fuguoai_core.api.devices_routes as dr
    from fuguoai_core.utils.bind_token_store import BindTokenStore

    bt_db = os.path.join(TMP, "bt_device_agent.db")
    bts = BindTokenStore(db_path=bt_db)
    bind_db = os.path.join(TMP, "bind_device_agent.db")
    iso_binding = DeviceBindingStore(db_path=bind_db)

    # 隔离依赖
    dr.get_bind_token_store = lambda: bts
    dr.DeviceBindingStore = lambda: iso_binding

    rec = bts.issue("u1", ttl=600)
    btok = rec["bind_token"]

    req = MagicMock()
    req.json = AsyncMock(return_value={"device_id": "DEVX", "bind_token": btok})
    req.headers = {}

    resp = await dr.handle_bind(req)
    body = json.loads(resp.body)
    assert body.get("ok") is True, body
    assert "device_token" in body and body["device_token"], body
    print("[P2] handle_bind returns device_token:", body["device_token"][:8], "...")

    # idempotent 分支也返回 device_token
    req2 = MagicMock()
    req2.json = AsyncMock(return_value={"device_id": "DEVX", "bind_token": btok})
    req2.headers = {}
    resp2 = await dr.handle_bind(req2)
    body2 = json.loads(resp2.body)
    assert body2.get("idempotent") is True and body2.get("device_token"), body2
    print("[P2] idempotent returns device_token ok")
    print("PART2_OK")


# ── Part3: _handle_auth device_ws_auth 分支 ──────────────────────────
class FakeWS:
    def __init__(self, headers):
        self.request = types.SimpleNamespace(headers=headers)
        self.closed = False

    async def send(self, m):
        pass

    async def close(self):
        self.closed = True


class FakeServer:
    def __init__(self, auth_enable, device_ws_auth_enable, allowed=None):
        self.auth_enable = auth_enable
        self.device_ws_auth_enable = device_ws_auth_enable
        self.allowed_devices = allowed or set()
        self.auth = types.SimpleNamespace(
            verify_token=lambda token, client_id=None, username=None: False
        )

    _handle_auth = None  # 注入真实方法


async def test_handle_auth():
    # monkeypatch DeviceBindingStore 指向测试库（_handle_auth 内部用默认路径，需对齐）
    import fuguoai_core.device_access as da
    db = os.path.join(TMP, "device_agent.db")
    test_binding = DeviceBindingStore(db_path=db)
    da.DeviceBindingStore = lambda: test_binding

    # 准备一个已激活设备 + 有效 token（复用 Part1 的库）
    b = DeviceBindingStore(db_path=db)
    b.issue_device_token("AABBCC", "goodtok")
    b.set_status("AABBCC", "active", user_id="u1")

    FakeServer._handle_auth = WebSocketServer._handle_auth

    # 场景A：device_ws_auth 开 + 有效 token → 放行
    srv = FakeServer(auth_enable=False, device_ws_auth_enable=True)
    wsA = FakeWS({"device-id": "AABBCC", "authorization": "Bearer goodtok"})
    await srv._handle_auth(wsA)
    print("[P3] A pass (valid device token allowed)")

    # 场景B：伪造 device-id + 他人有效 token → 拒绝
    wsB = FakeWS({"device-id": "SPOOFID", "authorization": "Bearer goodtok"})
    try:
        await srv._handle_auth(wsB)
        print("[P3] B FAIL (should raise)")
        raise SystemExit(1)
    except AuthenticationError:
        print("[P3] B pass (spoofed device-id rejected)")

    # 场景C：无 token → 拒绝
    wsC = FakeWS({"device-id": "AABBCC"})
    try:
        await srv._handle_auth(wsC)
        print("[P3] C FAIL (should raise)")
        raise SystemExit(1)
    except AuthenticationError:
        print("[P3] C pass (missing token rejected)")

    # 场景D：白名单设备始终放行（即便无 token）
    srv2 = FakeServer(auth_enable=False, device_ws_auth_enable=True, allowed={"AABBCC"})
    wsD = FakeWS({"device-id": "AABBCC"})
    await srv2._handle_auth(wsD)
    print("[P3] D pass (allowlist bypass)")

    # 场景E：两者都关 → 放行（向后兼容）
    srv3 = FakeServer(auth_enable=False, device_ws_auth_enable=False)
    wsE = FakeWS({"device-id": "WHATEVER"})
    await srv3._handle_auth(wsE)
    print("[P3] E pass (both disabled -> allow)")

    # 场景F：auth_key 通过（兼容老设备）+ device_ws_auth 开
    srv4 = FakeServer(auth_enable=True, device_ws_auth_enable=True)
    srv4.auth = types.SimpleNamespace(
        verify_token=lambda token, client_id=None, username=None: token == "legacyjwt"
    )
    wsF = FakeWS({"device-id": "OLDDEV", "authorization": "Bearer legacyjwt"})
    await srv4._handle_auth(wsF)
    print("[P3] F pass (legacy auth_key accepted)")
    print("PART3_OK")


async def main():
    test_store()
    await test_handle_bind()
    await test_handle_auth()
    print("ALL_M7_OK")


if __name__ == "__main__":
    asyncio.run(main())
