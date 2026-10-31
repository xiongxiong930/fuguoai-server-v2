"""PING 心跳处理器（fuguoai 独立实现）。

设备按约定周期发送 ``{"type": "ping"}``，服务端须回 ``{"type": "pong"}`` 并刷新
连接活跃时间以维持长连接。回应中的 ``type`` / ``timestamp`` 字段属于线上心跳协议
契约（通用心跳模式，非原始仓专有实现），须逐字兼容，故新实现与之格式一致。

本文件为按调用契约独立重写的 fuguoai 实现，发送走 ``conn.safe_send`` 而非
原始 ``conn.websocket.send``，与 fuguoai 连接抽象对齐；不再逐行对照原始源码。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict

from fuguoai_core.handle.textMessageHandler import TextMessageHandler
from fuguoai_core.handle.textMessageType import TextMessageType

TAG = __name__


class PingMessageHandler(TextMessageHandler):
    """处理设备发来的 PING，回送 PONG 以保持连接活跃。"""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.PING

    async def handle(self, conn, msg_json: Dict[str, Any]) -> None:
        """收到 PING 时回 PONG，并刷新连接活跃时间。"""
        # 检查是否启用了 WebSocket 心跳功能
        if not conn.config.get("enable_websocket_ping", False):
            conn.logger.debug("WebSocket心跳功能未启用，忽略PING消息")
            return

        try:
            conn.logger.debug("收到PING消息，发送PONG响应")
            conn.last_activity_time = time.time() * 1000

            # 构造 PONG 响应消息
            pong_message = {
                "type": "pong",
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            }

            # 发送 PONG 响应
            await conn.safe_send(json.dumps(pong_message))

        except Exception as exc:  # 单条心跳异常不应影响连接主流程
            conn.logger.error(f"处理PING消息时发生错误: {exc}")
