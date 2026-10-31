"""服务端指令消息处理器（按调用契约独立实现）。

处理管理端下发的服务端级指令，支持两类动作：

* ``update_config`` —— 热更新服务端配置（需 WebSocketServer 实例在场）；
* ``restart``       —— 重启服务端。

配置改动会立刻影响整服行为，因此本处理器要求请求携带 ``secret``，
且必须与 ``config["manager-api"]["secret"]`` 逐字一致，否则回错误报文并终止。

报文形状由管理端约定：``content`` 只在与具体动作绑定的响应里出现，
密钥校验失败的响应不带 ``content``——该差异是既有协议的一部分，重写已保留。
"""

import json
from typing import Any, Dict, Optional

from fuguoai_core.handle.textMessageHandler import TextMessageHandler
from fuguoai_core.handle.textMessageType import TextMessageType

TAG = __name__

_UPDATE_CONFIG = "update_config"
_RESTART = "restart"


class ServerTextMessageHandler(TextMessageHandler):
    """服务端指令消息处理器。"""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.SERVER

    # ------------------------------------------------------------------
    # 响应构造（原先五处重复「拼 dict → dumps → 发送」，此处收口）
    # ------------------------------------------------------------------
    @staticmethod
    def _payload(status: str, message: str, action: Optional[str] = None) -> str:
        """组装 server 响应报文；action 为 None 时不带 content 字段。"""
        body: Dict[str, Any] = {"type": "server", "status": status, "message": message}
        if action is not None:
            body["content"] = {"action": action}
        return json.dumps(body)

    async def _reply(self, conn, status: str, message: str,
                     action: Optional[str] = None) -> None:
        await conn.safe_send(self._payload(status, message, action))

    @staticmethod
    def _secret_accepted(conn, msg_json: Dict[str, Any]) -> bool:
        """请求密钥须与 manager-api.secret 逐字一致。"""
        posted = msg_json.get("content", {}).get("secret", "")
        expected = conn.config["manager-api"].get("secret", "")
        return posted == expected

    # ------------------------------------------------------------------
    # 动作
    # ------------------------------------------------------------------
    async def _apply_config_update(self, conn) -> None:
        """热更新服务端配置：先取实例，再更新，失败/异常统一回错误报文。"""
        try:
            if not conn.server:
                await self._reply(conn, "error", "无法获取服务器实例", _UPDATE_CONFIG)
                return
            if not await conn.server.update_config():
                await self._reply(conn, "error", "更新服务器配置失败", _UPDATE_CONFIG)
                return
            await self._reply(conn, "success", "配置更新成功", _UPDATE_CONFIG)
        except Exception as e:
            conn.logger.bind(tag=TAG).error(f"更新配置失败: {str(e)}")
            await self._reply(conn, "error", f"更新配置失败: {str(e)}", _UPDATE_CONFIG)

    async def handle(self, conn, msg_json: Dict[str, Any]) -> None:
        # 未启用「配置从 API 读取」时，服务端指令整体不处理
        if not conn.read_config_from_api:
            return

        if not self._secret_accepted(conn, msg_json):
            await self._reply(conn, "error", "服务器密钥验证失败")
            return

        action = msg_json["action"]
        if action == _UPDATE_CONFIG:
            await self._apply_config_update(conn)
        elif action == _RESTART:
            await conn.handle_restart(msg_json)
