import asyncio, sys, json
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "/opt/fuguoai-server")

# 顶层 import（同步阶段，避免嵌套事件循环）：
# helloHandle 的 import 链会在模块级触发 asyncio.run(load_config())，
# 必须在任何 asyncio.run 之外完成 import。
from fuguoai_core.handle.helloHandle import handleHelloMessage

PEER = "phone-Uc7d7cd08de20"

def _make_conn(device_id, is_phone):
    conn = MagicMock()
    conn.device_id = device_id
    conn.is_phone = is_phone
    conn.intercom_mode = False
    conn.welcome_msg = {}
    conn.config = {}
    logger = MagicMock()
    logger.bind.return_value = logger
    conn.logger = logger
    conn.safe_send = AsyncMock()
    return conn

async def test_device_side_resends():
    conn = _make_conn("94a9901a8818", False)
    with patch("fuguoai_core.utils.intercom_registry.get_intercom_registry") as g_reg, \
         patch("fuguoai_core.utils.connection_registry.get_connection_registry") as g_conn_reg, \
         patch("fuguoai_core.utils.intercom_call_store._set_intercom_cmd") as g_cmd:
        reg_inst = MagicMock()
        reg_inst.on_reconnect.return_value = PEER
        g_reg.return_value = reg_inst
        conn_reg_inst = MagicMock()
        conn_reg_inst.send_to_device = AsyncMock(return_value=True)
        g_conn_reg.return_value = conn_reg_inst
        g_cmd.side_effect = lambda p, a: {"type": "iot", "commands": [
            {"name": "DeviceControl", "method": "SetIntercom",
             "parameters": {"peer": p, "action": a}}]}

        await handleHelloMessage(conn, {"session": "intercom"})
        assert conn_reg_inst.send_to_device.called, "设备端重连必须重发 SetIntercom"
        dev, msg = conn_reg_inst.send_to_device.call_args[0]
        assert dev == "94a9901a8818", dev
        assert msg["commands"][0]["method"] == "SetIntercom"
        assert msg["commands"][0]["parameters"]["action"] == "start"
        assert msg["commands"][0]["parameters"]["peer"] == PEER
        print("PASS[1] 设备端重连 -> 重发 SetIntercom start (peer 正确)")

async def test_phone_side_skips():
    conn = _make_conn("phone-Uc7d7cd08de20", True)
    with patch("fuguoai_core.utils.intercom_registry.get_intercom_registry") as g_reg, \
         patch("fuguoai_core.utils.connection_registry.get_connection_registry") as g_conn_reg, \
         patch("fuguoai_core.utils.intercom_call_store._set_intercom_cmd") as g_cmd:
        reg_inst = MagicMock()
        reg_inst.on_reconnect.return_value = "94a9901a8818"
        g_reg.return_value = reg_inst
        conn_reg_inst = MagicMock()
        conn_reg_inst.send_to_device = AsyncMock(return_value=True)
        g_conn_reg.return_value = conn_reg_inst
        g_cmd.side_effect = lambda p, a: {"type": "iot", "method": "SetIntercom",
                                          "parameters": {"peer": p, "action": a}}

        await handleHelloMessage(conn, {"session": "intercom"})
        assert not conn_reg_inst.send_to_device.called, "手机端重连不应重发 SetIntercom"
        print("PASS[2] 手机端重连 -> 不重发 SetIntercom (与 _try_connect 契约一致)")

async def test_no_restore_no_resend():
    conn = _make_conn("94a9901a8818", False)
    with patch("fuguoai_core.utils.intercom_registry.get_intercom_registry") as g_reg, \
         patch("fuguoai_core.utils.connection_registry.get_connection_registry") as g_conn_reg:
        reg_inst = MagicMock()
        reg_inst.on_reconnect.return_value = None
        g_reg.return_value = reg_inst
        conn_reg_inst = MagicMock()
        conn_reg_inst.send_to_device = AsyncMock(return_value=True)
        g_conn_reg.return_value = conn_reg_inst

        await handleHelloMessage(conn, {"session": "intercom"})
        assert not conn_reg_inst.send_to_device.called, "无 grace 会话恢复不应重发"
        print("PASS[3] 无重连会话恢复 -> 不重发 SetIntercom")

if __name__ == "__main__":
    asyncio.run(test_device_side_resends())
    asyncio.run(test_phone_side_skips())
    asyncio.run(test_no_restore_no_resend())
    print("ALL HELLO RECONNECT VERIFY PASSED")
