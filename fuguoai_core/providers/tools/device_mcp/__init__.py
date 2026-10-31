"""设备端 MCP 工具集合。

对外暴露设备侧 MCP 的客户端、消息收发与执行器。
"""

from .mcp_client import MCPClient
from .mcp_handler import (
    send_mcp_message,
    handle_mcp_message,
    send_mcp_initialize_message,
    send_mcp_tools_list_request,
    call_mcp_tool,
)
from .mcp_executor import DeviceMCPExecutor

__all__ = [
    "MCPClient",
    "send_mcp_message",
    "handle_mcp_message",
    "send_mcp_initialize_message",
    "send_mcp_tools_list_request",
    "call_mcp_tool",
    "DeviceMCPExecutor",
]
