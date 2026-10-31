"""文本消息处理器抽象基类（复果自有实现）。"""

from abc import ABC, abstractmethod
from typing import Any, Dict

from fuguoai_core.handle.textMessageType import TextMessageType

TAG = __name__


class TextMessageHandler(ABC):
    """按消息类型分发的处理器接口。"""

    @abstractmethod
    async def handle(self, conn, msg_json: Dict[str, Any]) -> None:
        """处理一条文本消息。"""
        raise NotImplementedError

    @property
    @abstractmethod
    def message_type(self) -> TextMessageType:
        """本处理器负责的消息类型。"""
        raise NotImplementedError
