"""文本消息处理入口。

构造全局的「处理器注册表」与「消息处理器」单例，并把连接层收到的文本消息
转交给 ``message_processor.process_message`` 处理。具体消息类型（NFC 触碰 / 唤醒词 /
Hello 等）的路由与执行由 ``textHandler`` 子包内的各 handler 负责。
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler

from fuguoai_core.handle.textMessageHandlerRegistry import TextMessageHandlerRegistry
from fuguoai_core.handle.textMessageProcessor import TextMessageProcessor

TAG = __name__

# 全局处理器注册表：在模块加载时装配 textHandler 子包内的各消息 handler。
message_registry = TextMessageHandlerRegistry()

# 全局消息处理器：持有注册表引用，负责按消息类型分派到对应 handler。
message_processor = TextMessageProcessor(message_registry)


async def handleTextMessage(conn: "ConnectionHandler", message):
    """处理文本消息：转交给全局消息处理器执行分派。"""
    await message_processor.process_message(conn, message)
