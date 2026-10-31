"""UnifiedToolHandler 真进化离线测试（phase5 / 5-B）

钉死 H-A: handle_llm_function_call 的多函数调用(function_calls)由顺序 await 改为
asyncio.gather 并发执行 —— 延迟由 N×单次 降为 最慢单次; 单个失败经 return_exceptions
隔离, 不拖垮整批, 输出顺序与输入一致。

注意: 构造 UnifiedToolHandler 会触发 server_mcp/device_mcp 等导入(现已装 mcp 库),
用 MagicMock 当 conn 即可, 执行器内部不在此测试范围(只桩掉 tool_manager.execute_tool)。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import mcp_stub  # 必须在导入依赖 mcp 的模块之前注入 mcp 命名空间桩

from fuguoai_core.providers.tools.unified_tool_handler import UnifiedToolHandler
from fuguoai_plugins.register import Action, ActionResponse


def _build_handler():
    conn = MagicMock()
    conn.config = {}
    return UnifiedToolHandler(conn)


def test_ha_concurrent_and_isolated():
    h = _build_handler()
    order = []

    async def fake_exec(name, args):
        order.append(name)
        await asyncio.sleep(0.1)
        return ActionResponse(action=Action.RESPONSE, response=f"ok:{name}")

    h.tool_manager.execute_tool = AsyncMock(side_effect=fake_exec)

    data = {
        "function_calls": [
            {"name": "a", "arguments": {}},
            {"name": "b", "arguments": {}},
        ]
    }

    async def run():
        t0 = asyncio.get_event_loop().time()
        res = await h.handle_llm_function_call(MagicMock(), data)
        return res, asyncio.get_event_loop().time() - t0

    res, elapsed = asyncio.run(run())
    assert res.action == Action.RESPONSE, res.action
    # H-B 钉死: 多函数结果被正确合并(原 _combine_responses 误读 response.content 会崩)
    assert res.response and "ok:a" in res.response and "ok:b" in res.response, (
        f"多函数结果未正确合并: {res.response!r}"
    )
    assert set(order) == {"a", "b"}, order
    # 并发: 两个各 0.1s 的调用应 ~0.1s 完成, 而非顺序 ~0.2s
    assert elapsed < 0.2, f"多函数调用未并发, 耗时 {elapsed:.3f}s"
    print(f"[H-A] 2 个 0.1s 调用并发完成, 耗时 {elapsed * 1000:.0f}ms ✅")


def test_ha_error_isolation():
    h = _build_handler()
    order = []

    async def fake_exec2(name, args):
        order.append(name)
        if name == "b":
            raise RuntimeError("boom")
        return ActionResponse(action=Action.RESPONSE, response=f"ok:{name}")

    h.tool_manager.execute_tool = AsyncMock(side_effect=fake_exec2)
    data = {
        "function_calls": [
            {"name": "a", "arguments": {}},
            {"name": "b", "arguments": {}},
        ]
    }

    async def run():
        return await h.handle_llm_function_call(MagicMock(), data)

    res = asyncio.run(run())
    # _combine_responses: 任一 ERROR → 返回首个 ERROR(行为与原顺序执行一致)
    assert res.action == Action.ERROR, res.action
    assert "a" in order, "即使 b 失败, a 仍应被执行(失败隔离)"
    print("[H-A] 单点失败被隔离, 不拖垮整批 ✅")


if __name__ == "__main__":
    test_ha_concurrent_and_isolated()
    test_ha_error_isolation()
    print("\nALL UNIFIED HANDLER EVOLUTION TESTS PASS ✅")
