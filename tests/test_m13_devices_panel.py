"""M13 · 设备面板端点集成冒烟：验证 /api/v1/devices 返回在线状态 + 今日用量。

纯容器内运行（依赖 aiohttp / sqlite 单例）。用 monkeypatch 绕过 JWT 守卫，
构造一条测试设备(dev_m13)验证聚合逻辑，结束清理，不污染生产库。
"""
import asyncio
import json
import sys
import types


def _fake_log():
    noop = lambda *a, **k: None
    return types.SimpleNamespace(info=noop, warning=noop, error=noop, bind=lambda *a, **k: _fake_log())


sys.modules.setdefault("log", _fake_log())

import fuguoai_core.api.devices_routes as dr
from aiohttp.test_utils import make_mocked_request
from fuguoai_core.utils.connection_registry import get_connection_registry
from fuguoai_core.device_access import DeviceBindingStore
from fuguoai_core.utils.usage_store import get_usage_store

DEV = "dev_m13_panel"
SID = "sid_m13_panel"
UID = "u_m13_test"
FAKE_USER = {"user_id": UID, "openid": UID}


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" :: {extra}" if extra else ""))
    if not cond:
        raise SystemExit(f"M13 断言失败: {name}")


async def run():
    # 准备测试数据
    binding = DeviceBindingStore()
    binding.set_status(DEV, "active", user_id=UID)
    binding.set_device_name(DEV, "面板测试设备")
    usage = get_usage_store()
    usage.record_session_start(DEV, SID)
    usage.record_turn(DEV, SID, 10)
    usage.record_session_end(DEV, SID)
    get_connection_registry().mark_online(DEV, SID, "1.2.3.4")

    # 绕过 JWT 守卫
    orig_guard = dr.api_guard
    dr.api_guard = lambda req: FAKE_USER
    try:
        req = make_mocked_request("GET", "/api/v1/devices")
        resp = await dr.handle_list(req)
        body = json.loads(resp.text)
        print("RESP:", json.dumps(body, ensure_ascii=False))
        check("ok 字段", body.get("ok") is True)
        items = body.get("data", [])
        dev = next((d for d in items if d["device_id"] == DEV), None)
        check("返回测试设备", dev is not None)
        for f in ("device_id", "device_name", "status", "online",
                  "connected_at", "client_ip", "last_active", "today"):
            check(f"含字段 {f}", f in dev, str(dev))
        check("online=True", dev["online"] is True)
        check("client_ip 透传", dev["client_ip"] == "1.2.3.4")
        check("today.turns=1", dev["today"]["turns"] == 1, str(dev["today"]))
        check("today.sessions=1", dev["today"]["sessions"] == 1)
        check("last_active 为整数", isinstance(dev["last_active"], int))
        print("M13 PANEL TEST PASS")
    finally:
        dr.api_guard = orig_guard
        # 清理测试数据，避免污染
        get_connection_registry().mark_offline(DEV, SID)
        binding.set_status(DEV, "unbound", user_id=UID)


if __name__ == "__main__":
    asyncio.run(run())
