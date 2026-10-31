"""Hello 消息处理器（复果自有实现）。

消息路由链上的一环：设备建立会话时先发一条 ``hello``，由本 handler 识别并转交
``handleHelloMessage`` 完成握手（鉴权、参数下发、登记在线态等）。

本 handler 本身刻意保持极薄：只负责
1. 声明自己的消息类别（``TextMessageType.HELLO``），供注册表建立路由；
2. 把连接上下文与原始报文**原样**转交握手处理函数。

真正的握手语义（认证、设备登记、参数下发）都在 ``fuguoai_core.handle.helloHandle``
里，不在此处重复，避免两处逻辑漂移。
"""

from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler
from fuguoai_core.handle.helloHandle import handleHelloMessage
from fuguoai_core.handle.textMessageHandler import TextMessageHandler
from fuguoai_core.handle.textMessageType import TextMessageType


class HelloTextMessageHandler(TextMessageHandler):
    """Hello消息处理器"""

    @property
    def message_type(self) -> TextMessageType:
        """本处理器对应的消息类别（注册表据此路由）。"""
        return TextMessageType.HELLO

    async def handle(self, conn: "ConnectionHandler", msg_json: Dict[str, Any]) -> None:
        """转交握手处理：连接上下文与原始报文原样透传，不做改写。"""
        await handleHelloMessage(conn, msg_json)
