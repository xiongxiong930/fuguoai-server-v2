"""DeviceMCP 真进化离线测试（phase5 / 5-B）

钉死:
  D1  DeviceMCPExecutor: import json 提到模块顶部(原在 execute 内)。
  D2  DeviceMCPExecutor.execute: 设备返回非 JSON 字符串时, 原静默 pass 改为
      显式 warning 记录(解析失败可见, 便于诊断)。

依赖 mcp 的模块通过 mcp_stub 注入命名空间, 与真实 mcp 版本解耦。
"""
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

import mcp_stub  # 必须在导入依赖 mcp 的模块之前注入 mcp 命名空间桩

import fuguoai_core.providers.tools.device_mcp.mcp_executor as dev_mod
from fuguoai_core.providers.tools.device_mcp.mcp_executor import DeviceMCPExecutor
from fuguoai_plugins.register import Action


async def test_d2_nonjson_result_warns():
    conn = MagicMock()
    conn.mcp_client.is_ready = AsyncMock(return_value=True)
    conn.mcp_client.has_tool = MagicMock(return_value=True)

    executor = DeviceMCPExecutor(conn)
    dev_mod.logger = MagicMock()  # 捕获日志

    # call_mcp_tool 返回非 JSON 字符串 -> 触发 D2 告警分支
    with patch.object(
        dev_mod, "call_mcp_tool", new=AsyncMock(return_value="plain text, not json")
    ):
        res = await executor.execute(conn, "some_tool", {})

    # 非 JSON 结果仍按 REQLLM 回退(行为不变)
    assert res.action == Action.REQLLM, res.action
    # D2 钉死: 解析失败被显式记录 warning
    assert dev_mod.logger.bind.return_value.warning.called, (
        "D2: 设备返回非JSON结果应记录 warning"
    )
    print("[D2] 设备返回非JSON结果已显式告警(不再静默) ✅")


async def test_d1_json_import_at_top():
    # D1 钉死: json 在模块顶层导入(import json 不再出现在 execute 内)
    import inspect

    src = inspect.getsource(dev_mod.DeviceMCPExecutor.execute)
    assert "import json" not in src, "D1: execute 内不应再有 'import json'"
    assert "import json" in inspect.getsource(dev_mod), "D1: 模块顶部应 import json"
    print("[D1] import json 已提到模块顶部 ✅")


if __name__ == "__main__":
    asyncio.run(test_d2_nonjson_result_warns())
    asyncio.run(test_d1_json_import_at_top())
    print("\nALL DEVICE_MCP EVOLUTION TESTS PASS ✅")
