"""设备文本帧的总分拣入口。

设备侧经 WebSocket 推上来的每一帧文本都先落到这里，由它完成三件事：
把原始字符串解析成结构化载荷、按 `type` 把载荷交给注册表里对应的业务
处理器、以及在解析失败或类型未知时把这一帧计入连接级的畸形消息防护。

畸形防护是 fuguoai 自有的加固：同一个连接在窗口期内反复送来解析不了的
数据（固件 bug、抓包重放、线路串扰都可能造成）时，`note_malformed_message`
会在累计超阈值后主动断连，而不是无限回显把连接拖成慢速刷屏。因此
「计数并依返回值决定是否回显」这条分支不能简化成无条件回显。

对外契约与设备端协议绑定，改动需同步固件。
"""

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler
from fuguoai_core.handle.textMessageHandlerRegistry import TextMessageHandlerRegistry

TAG = __name__

# 畸形帧的两类来源，作为计数维度传给连接级防护
_UNROUTABLE = "unknown_type"
_UNPARSEABLE = "non_json"


class TextMessageProcessor:
    """按消息类型把设备文本帧分派给对应的业务处理器。"""

    def __init__(self, registry: TextMessageHandlerRegistry):
        # 公开属性：生产单例的自检与测试都直接读 processor.registry
        self.registry = registry

    async def process_message(self, conn: "ConnectionHandler", message: str) -> None:
        """处理一帧设备文本消息。

        与设备端对齐的三条分支：
        - 解析出 JSON 对象：取 `type` 交给注册表；无对应处理器记为未知类型。
        - 解析出 JSON 整数：固件用它做透传序号，原样回发。
        - 解析失败：记为畸形帧并计数，防护未触发才回显原文。
        """
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            conn.logger.bind(tag=TAG).error(f"文本帧不是合法 JSON，原样透传前先计数：{message}")
            await self._guard_then_echo(conn, _UNPARSEABLE, message)
            return

        if isinstance(payload, dict):
            await self._dispatch_by_type(conn, message, payload)
        elif isinstance(payload, int):
            conn.logger.bind(tag=TAG).info(f"设备回传透传序号：{message}")
            await conn.safe_send(message)

    async def _dispatch_by_type(
        self, conn: "ConnectionHandler", raw: str, payload: dict
    ) -> None:
        """结构化帧：按 type 查注册表并执行命中的处理器。"""
        message_type = payload.get("type")
        conn.logger.bind(tag=TAG).info(f"分发设备文本帧 type={message_type}：{raw}")

        handler = self.registry.get_handler(message_type)
        if handler is None:
            # 未知类型只计数不回显：回显会把固件不认识的帧原样弹回去，
            # 在双向链路里容易和设备端形成回音。
            conn.logger.bind(tag=TAG).error(f"无处理器可承接该类型：{raw}")
            await conn.note_malformed_message(_UNROUTABLE, raw)
            return

        await handler.handle(conn, payload)

    async def _guard_then_echo(
        self, conn: "ConnectionHandler", kind: str, message: str
    ) -> None:
        """畸形帧：先计数，防护未触发才把原文回发给设备。

        `note_malformed_message` 返回真表示防护已介入（连接可能已被关闭），
        此时再回显既无意义也会写进一条已关闭的 socket。
        """
        if await conn.note_malformed_message(kind, message):
            return
        await conn.safe_send(message)
