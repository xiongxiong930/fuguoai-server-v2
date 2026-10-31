"""设备文本消息处理器注册表（复果自有实现）。

把设备经 WebSocket 推来的各类文本消息（hello / 中断 / 聆听 / 物联 / MCP /
服务器 / ping，以及复果自有的 AEC 参考信号 / 会话内唤醒词 / 主动播报 /
设备日程 / NFC 触碰 / NFC 认领）映射到对应的业务处理器。

装配关系
--------
本模块在导入时即构造全局单例（见 ``fuguoai_core/handle/textHandle.py``），
由 ``TextMessageProcessor`` 持有并按消息类型分派 ⇒
**注册表在进程内只装配一次，handler 实例全局复用**。

注意：本模块顶层导入 ``textHandler`` 子包内的全部 handler，
导入本模块即会触发该子包的整条导入链。
"""

from typing import Dict, Optional

from fuguoai_core.handle.textHandler.abortMessageHandler import AbortTextMessageHandler
from fuguoai_core.handle.textHandler.helloMessageHandler import HelloTextMessageHandler
from fuguoai_core.handle.textHandler.iotMessageHandler import IotTextMessageHandler
from fuguoai_core.handle.textHandler.listenMessageHandler import ListenTextMessageHandler
from fuguoai_core.handle.textHandler.mcpMessageHandler import McpTextMessageHandler
from fuguoai_core.handle.textMessageHandler import TextMessageHandler
from fuguoai_core.handle.textMessageType import TextMessageType
from fuguoai_core.handle.textHandler.serverMessageHandler import ServerTextMessageHandler
from fuguoai_core.handle.textHandler.pingMessageHandler import PingMessageHandler
from fuguoai_core.handle.textHandler.audioPlaybackRefMessageHandler import AudioPlaybackRefTextMessageHandler
from fuguoai_core.handle.textHandler.wakeWordInSessionMessageHandler import WakeWordInSessionTextMessageHandler
from fuguoai_core.handle.textHandler.speakRequestHandler import SpeakRequestTextMessageHandler
from fuguoai_core.handle.textHandler.schedulePullMessageHandler import SchedulePullMessageHandler
from fuguoai_core.handle.textHandler.nfcTapMessageHandler import NfcTapTextMessageHandler
from fuguoai_core.handle.textHandler.nfcClaimMessageHandler import NfcClaimTextMessageHandler
from fuguoai_core.handle.textHandler.v30MessageHandler import (
    AuthTextMessageHandler,
    SchedulePushTextMessageHandler,
    ClockTextMessageHandler,
    IntercomReadyTextMessageHandler,
)

TAG = __name__


class TextMessageHandlerRegistry:
    """设备文本消息处理器注册表：按消息类型字符串索引处理器实例。

    对外三项能力：
    - ``register_handler``：登记（或覆盖）一个处理器；
    - ``get_handler``：按消息类型取处理器，未命中返回 ``None``；
    - ``get_supported_types``：列出当前已注册的消息类型（列表副本）。
    """

    def __init__(self):
        # 消息类型字符串 -> 处理器实例
        self._handlers: Dict[str, TextMessageHandler] = {}
        self._register_default_handlers()

    def _register_default_handlers(self) -> None:
        """登记全部默认处理器。

        顺序即 ``get_supported_types()`` 的返回顺序：
        基础七类（hello / 中断 / 聆听 / 物联 / MCP / 服务器 / ping），
        再接复果自有的六类（AEC 参考信号 / 会话内唤醒词 / 主动播报 /
        设备日程 / NFC 触碰 / NFC 认领），最后接 v3.0 新增/别名类型。
        """
        handlers = [
            HelloTextMessageHandler(),
            AbortTextMessageHandler(),
            ListenTextMessageHandler(),
            IotTextMessageHandler(),
            McpTextMessageHandler(),
            ServerTextMessageHandler(),
            PingMessageHandler(),
            AudioPlaybackRefTextMessageHandler(),
            WakeWordInSessionTextMessageHandler(),
            SpeakRequestTextMessageHandler(),
            SchedulePullMessageHandler(),
            NfcTapTextMessageHandler(),
            NfcClaimTextMessageHandler(),
        ]

        for handler in handlers:
            self.register_handler(handler)

        # v3.0 别名：把既有处理器的语义暴露为 v3.0 直白类型名，向后兼容。
        # 复用上方同一实例，避免重复装配。
        abort = self._handlers[TextMessageType.ABORT.value]
        listen = self._handlers[TextMessageType.LISTEN.value]
        wake = self._handlers[TextMessageType.WAKE_WORD_IN_SESSION.value]
        self.register_alias(TextMessageType.ABORT_SPEAKING.value, abort)
        self.register_alias(TextMessageType.START_LISTENING.value, listen)
        self.register_alias(TextMessageType.STOP_LISTENING.value, listen)
        self.register_alias(TextMessageType.WAKE_WORD_DETECTED.value, wake)
        # pong 复用 ping 处理器的"刷新活跃"语义（设备应答服务端 ping）
        self.register_alias(TextMessageType.PONG.value, PingMessageHandler())

        # v3.0 新增、无 v1/v2 等价物的处理器
        schedule_push = SchedulePushTextMessageHandler()
        self.register_handler(schedule_push)
        # schedule_mirror 与 schedule_push 语义一致（设备请求云端以设备为镜像源）
        self.register_alias(TextMessageType.SCHEDULE_MIRROR.value, schedule_push)
        for handler in (
            AuthTextMessageHandler(),
            ClockTextMessageHandler(),
            IntercomReadyTextMessageHandler(),
        ):
            self.register_handler(handler)

    def register_handler(self, handler: TextMessageHandler) -> None:
        """登记处理器：以其 ``message_type.value`` 为键。

        同键重复登记时**后注册者覆盖先注册者**（不报错、不追加），
        因此调用方可安全地替换既有处理器。
        """
        self._handlers[handler.message_type.value] = handler

    def register_alias(self, type_str: str, handler: TextMessageHandler) -> None:
        """登记类型别名：把同一处理器实例挂到额外的类型字符串下。

        用于 v3.0 语义别名（如 ``start_listening`` → LISTEN 处理器），
        不改变主类型路由，仅扩展可被识别的 ``type`` 取值。
        """
        if not type_str:
            return
        self._handlers[type_str] = handler

    def get_handler(self, message_type: str) -> Optional[TextMessageHandler]:
        """按消息类型取处理器；未命中返回 ``None``（不抛 KeyError）。"""
        return self._handlers.get(message_type)

    def get_supported_types(self) -> list:
        """列出已注册的消息类型列表（**列表副本**，按注册顺序）。

        返回副本而非字典视图，调用方修改返回值不会影响内部注册表。
        """
        return list(self._handlers.keys())
