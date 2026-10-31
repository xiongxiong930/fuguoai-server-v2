"""ServerMCP 真进化离线测试（phase5 / 5-B）

钉死:
  S1  ServerMCPClient.call_tool: 跨线程/同线程调用加 asyncio.wait_for 超时边界,
      底层会话调用挂死时不再永久冻结调用方(原代码跨线程分支无超时)。
  S2  ServerMCPClient._worker: 异常用 logger.exception 记录完整 traceback(原只记 str(e))。
  S3  ServerMCPManager.initialize_servers: asyncio.gather 加 return_exceptions,
      单服务初始化失败隔离, 不连带取消其余。
  S4  cleanup/cleanup_all: 冗余的 (asyncio.TimeoutError, Exception) 简化为 Exception,
      关闭单个失败客户端不向上传播。

依赖 mcp 的模块通过 mcp_stub 注入命名空间, 与本仓实际目标 mcp 版本解耦。
"""
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

import mcp_stub  # 必须在导入依赖 mcp 的模块之前注入 mcp 命名空间桩

import fuguoai_core.providers.tools.server_mcp.mcp_client as client_mod
import fuguoai_core.providers.tools.server_mcp.mcp_manager as mgr_mod
from fuguoai_core.providers.tools.server_mcp.mcp_client import ServerMCPClient
from fuguoai_core.providers.tools.server_mcp.mcp_manager import ServerMCPManager


def _client_with_session(loop):
    """构造一个 session 已就绪、worker 在同一 loop 的 ServerMCPClient。"""
    c = ServerMCPClient({"url": "http://example"})
    c._worker_task = MagicMock()
    c._worker_task.get_loop.return_value = loop
    c.session = MagicMock()
    return c


async def test_s1_call_tool_timeout():
    loop = asyncio.get_running_loop()
    c = _client_with_session(loop)

    async def _hang(*a, **k):
        await asyncio.sleep(10)
        return "never"

    c.session.call_tool = _hang
    try:
        await c.call_tool("tool", {}, call_timeout=0.1)
        raise AssertionError("call_tool 应在超时后抛出 asyncio.TimeoutError")
    except asyncio.TimeoutError:
        pass
    print("[S1] 跨线程/同线程调用超时边界生效 ✅")

    # 快速路径正常返回
    c.session.call_tool = AsyncMock(return_value="ok-result")
    res = await c.call_tool("tool", {}, call_timeout=5)
    assert res == "ok-result", res
    print("[S1] 正常调用按时返回 ✅")


async def test_s2_worker_logs_traceback():
    c = ServerMCPClient({"url": "http://example"})
    c.logger = MagicMock()  # 捕获日志调用
    # _worker 会尝试 enter streamablehttp_client(object 占位) -> 立即抛 TypeError,
    # 触发 except 分支(S2)。
    task = asyncio.create_task(c._worker())
    try:
        await task
    except Exception:
        pass
    # S2 钉死: 失败已设 ready_evt(不永久挂起初始化) 且记录 traceback
    assert c._ready_evt.is_set(), "worker 失败应 set ready_evt, 否则 initialize 永久挂起"
    assert c.logger.bind.return_value.exception.called, "worker 异常应走 logger.exception(带 traceback)"
    print("[S2] worker 异常完整 traceback 已记录 ✅")


async def test_s3_initialize_servers_return_exceptions():
    conn = MagicMock()
    mgr = ServerMCPManager(conn)
    mgr.load_config = lambda: {"s1": {"url": "http://a"}, "s2": {"url": "http://b"}}

    captured = {}
    orig = asyncio.gather

    def _fake_gather(*tasks, **kw):
        captured.update(kw)
        return orig(*tasks, **kw)

    with patch.object(mgr_mod.asyncio, "gather", _fake_gather):
        # _init_server 内部已吞异常, 这里只验证 gather 用了 return_exceptions
        await mgr.initialize_servers()

    assert captured.get("return_exceptions") is True, (
        f"S3: initialize_servers 的 gather 未加 return_exceptions: {captured}"
    )
    print("[S3] initialize_servers 并发初始化隔离单点失败 ✅")


async def test_s4_cleanup_all_swallows():
    conn = MagicMock()
    mgr = ServerMCPManager(conn)
    bad = MagicMock()
    bad.cleanup = AsyncMock(side_effect=RuntimeError("boom"))
    mgr.clients["bad"] = bad
    # S4: 单个客户端关闭失败不应向上传播, 且从 clients 清除
    await mgr.cleanup_all()
    assert "bad" not in mgr.clients, "cleanup_all 应清除已处理的客户端"
    print("[S4] 单个客户端关闭失败被吞掉且已清除 ✅")


if __name__ == "__main__":
    asyncio.run(test_s1_call_tool_timeout())
    asyncio.run(test_s2_worker_logs_traceback())
    asyncio.run(test_s3_initialize_servers_return_exceptions())
    asyncio.run(test_s4_cleanup_all_swallows())
    print("\nALL SERVER_MCP EVOLUTION TESTS PASS ✅")
