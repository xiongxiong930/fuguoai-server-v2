"""设备端IoT执行器真进化离线测试（钉入回归门禁 L3）。

对应 tools/PHASE5_PLAN.md 5-B 的 device_iot 组真实进化:
- D5: register_iot_tools 时建立 tool_name -> (kind, device, sub) 路由表, execute 直接查表。
     修复"设备名含下划线(如 living_room)被 split('_') 错误切分导致查不到设备"的 bug,
     且把每次调用 O(N) 线性遍历 iot_descriptors 降为 O(1) 定位。
- D3: execute 异常不再静默吞掉, 显式记录 warning 便于诊断(仍按失败回退)。

注意: fuguoai_plugins.register 导入期有 asyncio 副作用, 故重模块导入放在文件顶层。
"""

import asyncio
import json
from unittest.mock import patch, MagicMock, AsyncMock

from fuguoai_plugins.register import Action
from fuguoai_core.providers.tools.device_iot.iot_executor import DeviceIoTExecutor
from fuguoai_core.providers.tools.device_iot import iot_executor as iot_exec_mod


class _Log:
    """记录 warning 是否被调用的假 logger。"""

    def __init__(self):
        self.warned = False

    def bind(self, **kw):
        return self

    def warning(self, *a, **k):
        self.warned = True

    def info(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass


def _make_conn():
    class _Dev:
        def __init__(self, props, methods):
            self.properties = props
            self.methods = methods

    descriptors = {
        "living_room": _Dev(
            [{"name": "power", "value": "on"}],
            [{"name": "turn_on", "description": "开"}],
        ),
        "bedroom": _Dev(
            [{"name": "temp", "value": "22"}],
            [{"name": "turn_off", "description": "关"}],
        ),
    }
    conn = MagicMock()
    conn.iot_descriptors = descriptors
    conn.safe_send = AsyncMock()
    return conn


def _lr_desc():
    return {
        "name": "living_room",
        "description": "客厅",
        "properties": {"power": {"description": "电源"}},
        "methods": {"turn_on": {"description": "开"}},
    }


async def test_get_underscore_device_routed():
    """D5: 设备名含下划线时, get 工具仍能正确路由到该设备(原实现会 split 错切成 'living')。"""
    conn = _make_conn()
    ex = DeviceIoTExecutor(conn)
    ex.register_iot_tools([_lr_desc()])

    resp = await ex.execute(conn, "get_living_room_power", {})
    assert resp.action == Action.RESPONSE, resp
    assert "on" in resp.response, resp.response


async def test_control_underscore_device_routed_and_sent():
    """D5: 设备名含下划线时, control 工具正确路由且下发命令含原始设备名。"""
    conn = _make_conn()
    ex = DeviceIoTExecutor(conn)
    ex.register_iot_tools([_lr_desc()])

    resp = await ex.execute(conn, "living_room_turn_on", {})
    assert resp.action == Action.REQLLM, resp
    assert conn.safe_send.await_count == 1
    sent = json.loads(conn.safe_send.call_args[0][0])
    assert sent["type"] == "iot"
    cmd = sent["commands"][0]
    assert cmd["name"] == "living_room", cmd  # 关键: 不被错切成 'living'
    assert cmd["method"] == "turn_on", cmd


async def test_multiple_devices_no_cross_talk():
    """D5: 多设备注册后互不串扰。"""
    conn = _make_conn()
    ex = DeviceIoTExecutor(conn)
    ex.register_iot_tools(
        [
            _lr_desc(),
            {
                "name": "bedroom",
                "description": "卧室",
                "properties": {"temp": {"description": "温度"}},
                "methods": {"turn_off": {"description": "关"}},
            },
        ]
    )
    r1 = await ex.execute(conn, "get_bedroom_temp", {})
    assert r1.action == Action.RESPONSE and "22" in r1.response, r1
    r2 = await ex.execute(conn, "get_living_room_power", {})
    assert r2.action == Action.RESPONSE and "on" in r2.response, r2


async def test_execute_logs_and_falls_back_on_error():
    """D3: 执行异常时显式记录 warning, 且仍按失败回退返回 ERROR(不崩溃)。"""
    conn = _make_conn()
    conn.safe_send = AsyncMock(side_effect=RuntimeError("send fail"))
    ex = DeviceIoTExecutor(conn)
    ex.register_iot_tools([_lr_desc()])

    log = _Log()
    with patch.object(iot_exec_mod, "logger", log):
        resp = await ex.execute(conn, "living_room_turn_on", {})

    assert resp.action == Action.ERROR, resp
    assert log.warned is True, "异常应被显式记录 warning"


async def test_unknown_tool_returns_notfound():
    """未注册工具返回 NOTFOUND。"""
    conn = _make_conn()
    ex = DeviceIoTExecutor(conn)
    ex.register_iot_tools([_lr_desc()])

    resp = await ex.execute(conn, "no_such_tool", {})
    assert resp.action == Action.NOTFOUND, resp


def main():
    tests = [
        test_get_underscore_device_routed,
        test_control_underscore_device_routed_and_sent,
        test_multiple_devices_no_cross_talk,
        test_execute_logs_and_falls_back_on_error,
        test_unknown_tool_returns_notfound,
    ]
    failed = 0
    for t in tests:
        try:
            asyncio.run(t())
            print(f"[PASS] {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"[FAIL] {t.__name__}: {e}")
    if failed:
        print(f"\n{len(tests) - failed}/{len(tests)} passed, {failed} FAILED")
        raise SystemExit(1)
    print(f"\nALL {len(tests)} PASSED")


if __name__ == "__main__":
    main()
