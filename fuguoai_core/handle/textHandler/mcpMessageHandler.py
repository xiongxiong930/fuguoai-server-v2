"""MCP 文本消息处理器（复果自有实现）。

设备经 WebSocket 上报的 MCP 消息（NFC 碰卡 / 领卡 / 工具调用结果等）由此处理：
取出 payload 后异步交给 ``device_mcp`` 的 ``handle_mcp_message`` 执行，不阻塞消息分发循环。
"""

import asyncio
from typing import Any, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler

from fuguoai_core.handle.textMessageHandler import TextMessageHandler
from fuguoai_core.handle.textMessageType import TextMessageType
from fuguoai_core.providers.tools.device_mcp import handle_mcp_message


class McpTextMessageHandler(TextMessageHandler):
    """MCP 消息处理器。"""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.MCP

    async def handle(self, conn: "ConnectionHandler", msg_json: Dict[str, Any]) -> None:
        if "payload" in msg_json:
            asyncio.create_task(
                handle_mcp_message(conn, conn.mcp_client, msg_json["payload"])
            )
