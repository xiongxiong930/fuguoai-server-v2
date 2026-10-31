"""MCP接入点子系统真进化离线测试（钉入回归门禁 L3）。

对应 tools/PHASE5_PLAN.md 5-B 的 mcp_endpoint 组三处真实进化:
- E1 handler.connect_mcp_endpoint: websockets.connect 加 open_timeout/ping_interval/ping_timeout/close_timeout
- E2 client.close: 异常安全 + 幂等 + is_connected 可观测
- E3 executor.execute: 函数内 import json 提顶 + 结果 JSON 解析失败显式告警(不再静默 pass)

注意: fuguoai_plugins.register 导入期有 asyncio 副作用, 故所有重模块导入放在文件顶层
(在 main()/asyncio.run 之前、无 running loop 时一次性吸收), 测试函数内不再 import。
"""

import asyncio
import json
from unittest.mock import patch, MagicMock, AsyncMock

# 顶层导入: 触发并吸收 fuguoai_plugins.register 的 asyncio 副作用(导入期无 running loop)
from fuguoai_plugins.register import Action
from fuguoai_core.providers.tools.mcp_endpoint import mcp_endpoint_handler as h
from fuguoai_core.providers.tools.mcp_endpoint.mcp_endpoint_client import (
    MCPEndpointClient,
)
from fuguoai_core.providers.tools.mcp_endpoint.mcp_endpoint_executor import (
    MCPEndpointExecutor,
)


class _FakeWS:
    """无外部依赖的假 websocket: send 为 no-op, 迭代立即结束。"""

    def __init__(self):
        self.send = AsyncMock()

    def __aiter__(self):
        async def _gen():
            return
            yield

        return _gen()


async def test_connect_passes_robustness_args():
    """E1: 连接必须传入硬性超时与心跳守卫参数。"""
    captured = {}

    async def fake_connect(url, **kw):
        captured.update(kw)
        return _FakeWS()

    with patch.object(h.websockets, "connect", side_effect=fake_connect):
        client = await h.connect_mcp_endpoint("ws://example.com/x", conn=None)
        await asyncio.sleep(0.02)  # 让后台 listener 协程跑完, 避免任务泄漏告警

    assert client is not None, "连接应成功返回客户端"
    assert captured.get("open_timeout") == 10, f"缺 open_timeout: {captured}"
    assert captured.get("ping_interval") == 20, f"缺 ping_interval: {captured}"
    assert captured.get("ping_timeout") == 10, f"缺 ping_timeout: {captured}"
    assert captured.get("close_timeout") == 10, f"缺 close_timeout: {captured}"


async def test_client_close_idempotent_and_safe():
    """E2: close 在异常时吞掉且幂等, is_connected 反映真实状态。"""
    c = MCPEndpointClient()

    # 第一次 close: websocket.close() 抛异常, 不应冒泡
    bad_ws = MagicMock()
    bad_ws.close = AsyncMock(side_effect=RuntimeError("already closed"))
    c.websocket = bad_ws
    await c.close()
    assert c.is_connected is False, "close 后应标记未连接"

    # 第二次 close: 幂等 no-op, 仍不崩
    await c.close()
    assert c.is_connected is False


async def test_executor_routes_json_action():
    """E3: 结果含 action 字段时正确路由到对应 Action。"""
    conn = MagicMock()
    conn.mcp_endpoint_client = MagicMock()
    conn.mcp_endpoint_client.is_ready = AsyncMock(return_value=True)
    ex = MCPEndpointExecutor(conn)

    fake_result = json.dumps({"action": "RESPONSE", "response": "ok"})
    with patch(
        "fuguoai_core.providers.tools.mcp_endpoint.mcp_endpoint_executor.call_mcp_endpoint_tool",
        new=AsyncMock(return_value=fake_result),
    ):
        resp = await ex.execute(conn, "tool_x", {})

    assert resp.action == Action.RESPONSE, f"应路由到 RESPONSE, 实际 {resp.action}"
    assert resp.response == "ok"


async def test_executor_nonjson_falls_back_to_reqllm():
    """E3: 非 JSON 结果仍按原文返回 REQLLM(失败可见, 不破坏回退)。"""
    conn = MagicMock()
    conn.mcp_endpoint_client = MagicMock()
    conn.mcp_endpoint_client.is_ready = AsyncMock(return_value=True)
    ex = MCPEndpointExecutor(conn)

    with patch(
        "fuguoai_core.providers.tools.mcp_endpoint.mcp_endpoint_executor.call_mcp_endpoint_tool",
        new=AsyncMock(return_value="just plain text"),
    ):
        resp = await ex.execute(conn, "tool_x", {})

    assert resp.action == Action.REQLLM, f"非JSON应回退 REQLLM, 实际 {resp.action}"
    assert resp.result == "just plain text"


async def test_executor_uninitialized_returns_error():
    """未初始化客户端时返回 ERROR, 不抛异常。"""
    conn = MagicMock()
    conn.mcp_endpoint_client = None
    ex = MCPEndpointExecutor(conn)

    resp = await ex.execute(conn, "tool_x", {})
    assert resp.action == Action.ERROR, f"未初始化应 ERROR, 实际 {resp.action}"


def main():
    tests = [
        test_connect_passes_robustness_args,
        test_client_close_idempotent_and_safe,
        test_executor_routes_json_action,
        test_executor_nonjson_falls_back_to_reqllm,
        test_executor_uninitialized_returns_error,
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
