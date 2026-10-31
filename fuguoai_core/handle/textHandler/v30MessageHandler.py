"""v3.0 协议新增/别名消息处理器（复果自有实现）。

v3.0 线协议由**后端定义、固件对齐**（见 ``websocket_server.py`` 契约声明）。本模块承载
v3.0 中**无 v1/v2 等价物**的新类型；语义别名（start_listening/stop_listening/
abort_speaking/wake_word_detected/pong）在 ``textMessageHandlerRegistry`` 里直接复用
既有处理器实例，不在此重复。

新增类型：
- ``auth``：v3.0 鉴权消息，与 HTTP Header 鉴权**双认**（token 格式沿用旧
  ``<sig>.<ts>`` HMAC；device_ws_auth 开启时校验 device_token）。
- ``schedule_push`` / ``schedule_mirror``：设备→云端全量镜像（M1），落 ``ScheduleMirrorStore``。
- ``clock``：时钟同步，服务端回当前 unix 毫秒，供固件做时钟对齐。
- ``intercom_ready``：对讲链路就绪确认（对讲仍由 hello session:intercom 建立，此处仅确认）。

向后兼容：这些类型仅在 v3.0 固件出现；v1/v2 固件不发送，不影响既有链路。
"""

import json
import time
from typing import Any, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler

from fuguoai_core.handle.textMessageHandler import TextMessageHandler
from fuguoai_core.handle.textMessageType import TextMessageType
from fuguoai_core.utils.util import normalize_device_id

TAG = __name__


class AuthTextMessageHandler(TextMessageHandler):
    """v3.0 鉴权消息：与 Header 鉴权双认。

    固件可在 WS 握手阶段用 HTTP Header（device-id/client-id/authorization）鉴权，
    也可在建连后第一条 JSON 消息里发 ``{"type":"auth","token":"<sig>.<ts>"}``。
    两种都放行；device_ws_auth 开启时校验 device_token，auth_key 开启时校验 JWT。
    """

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.AUTH

    async def handle(self, conn: "ConnectionHandler", msg_json: Dict[str, Any]) -> None:
        server = getattr(conn, "server", None)
        auth_enable = bool(getattr(server, "auth_enable", False))
        dwa_enable = bool(getattr(server, "device_ws_auth_enable", False))
        # 两种机制都未启用 → 鉴权本就放行，直接标记通过。
        if not (auth_enable or dwa_enable):
            conn._v30_auth_ok = True
            return

        token = (msg_json.get("token") or "").strip()
        client_id = (msg_json.get("client_id") or "").strip()
        # 优先用消息自带 device_id（归一化），否则回落连接层已解析的 device_id。
        device_id = normalize_device_id(msg_json.get("device_id")) or conn.device_id

        if token.lower().startswith("bearer "):
            token = token[7:].strip()

        # 白名单设备始终放行
        allowed = getattr(server, "allowed_devices", set())
        if allowed and device_id in allowed:
            conn._v30_auth_ok = True
            # 同样须回写连接层设备标识：header-less 连接时 conn.device_id 为空，
            # 不回写会导致下游风暴熔断 / 在线判定 / 路由拿不到设备标识。
            if device_id:
                conn.device_id = normalize_device_id(device_id)
            conn.logger.bind(tag=TAG).info(
                f"[v3.0][auth] 设备 {device_id} 白名单放行"
            )
            return

        ok = False
        # 1) 既有 auth_key JWT
        if auth_enable and token:
            try:
                auth = getattr(server, "auth", None)
                if auth and auth.verify_token(token, client_id=client_id, username=device_id):
                    ok = True
            except Exception:
                ok = False
        # 2) M7 device_ws_auth：token 须与 device_id 绑定且设备已激活
        if not ok and dwa_enable:
            if device_id and token:
                try:
                    from fuguoai_core.device_access import DeviceBindingStore

                    if DeviceBindingStore().verify_device_token(
                        normalize_device_id(device_id), token
                    ):
                        ok = True
                except Exception:
                    ok = False

        if ok:
            conn._v30_auth_ok = True
            # 补全连接层设备标识：固件无 header 时 device_id 仅在 JSON auth 消息携带，
            # 须写回 conn 供下游风暴熔断 / 在线判定 / 路由 / 设备存储使用。
            conn.device_id = normalize_device_id(device_id)
            conn.logger.bind(tag=TAG).info(
                f"[v3.0][auth] 设备 {device_id} JSON auth 通过"
            )
            return

        conn.logger.bind(tag=TAG).warning(
            f"[v3.0][auth] 设备 {device_id} JSON auth 失败，关闭连接"
        )
        try:
            await conn.websocket.close()
        except Exception:
            pass


class SchedulePushTextMessageHandler(TextMessageHandler):
    """v3.0 设备→云端全量镜像（M1）。

    固件发起 ``schedule_push`` / ``schedule_mirror``，把本地日程全量上报；
    后端以 LWW 合并进云端命名空间（source=cloud 由存储层标记）。回 ``schedule_ack``。
    """

    @property
    def message_type(self) -> TextMessageType:
        # schedule_mirror 复用本处理器（构造时再注册一次别名）
        return TextMessageType.SCHEDULE_PUSH

    async def handle(self, conn: "ConnectionHandler", msg_json: Dict[str, Any]) -> None:
        device_id = normalize_device_id(conn.device_id) if conn and conn.device_id else None
        if not device_id:
            return
        items = (
            msg_json.get("tasks")
            or msg_json.get("schedule")
            or msg_json.get("items")
            or []
        )
        try:
            from fuguoai_core.utils.schedule_mirror_store import get_schedule_mirror_store

            store = get_schedule_mirror_store()
            # merge_full_sync：设备全量上行 → 云端命名空间 LWW 合并
            merged = store.merge_full_sync(device_id, items or [])
            ack = {
                "type": "schedule_ack",
                "accepted": True,
                "count": int(merged if isinstance(merged, int) else len(items or [])),
            }
            await conn.safe_send(json.dumps(ack, ensure_ascii=False))
            conn.logger.bind(tag=TAG).info(
                f"[v3.0][schedule] device={device_id} push 合并 {ack['count']} 条"
            )
        except Exception as e:  # noqa: BLE001
            conn.logger.bind(tag=TAG).warning(
                f"[v3.0][schedule] push 合并失败(device={device_id}): {e}"
            )


class ClockTextMessageHandler(TextMessageHandler):
    """v3.0 时钟同步：服务端回当前 unix 毫秒，供固件做时钟对齐。

    固件可带 ``device_time``（其本地 unix 毫秒）上行，服务端原样回显并附 ``server_time``，
    固件据此算偏移。不驱动任何业务逻辑。
    """

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.CLOCK

    async def handle(self, conn: "ConnectionHandler", msg_json: Dict[str, Any]) -> None:
        try:
            resp = {
                "type": "clock",
                "server_time": int(time.time() * 1000),
                "device_time": msg_json.get("device_time"),
            }
            await conn.safe_send(json.dumps(resp, ensure_ascii=False))
            conn.logger.bind(tag=TAG).debug(
                f"[v3.0][clock] 回 server_time={resp['server_time']}"
            )
        except Exception as e:  # noqa: BLE001
            conn.logger.bind(tag=TAG).warning(f"[v3.0][clock] 回包失败: {e}")


class IntercomReadyTextMessageHandler(TextMessageHandler):
    """v3.0 对讲链路就绪确认。

    对讲会话仍由 hello ``session:intercom`` 建立（v1/v2/v3 一致）；本类型供固件在
    对讲音频通道就绪后显式确认，服务端记录并回 ack。不改变既有对讲透传链路。
    """

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.INTERCOM_READY

    async def handle(self, conn: "ConnectionHandler", msg_json: Dict[str, Any]) -> None:
        is_intercom = bool(getattr(conn, "intercom_mode", False))
        conn.logger.bind(tag=TAG).info(
            f"[v3.0][intercom_ready] device={conn.device_id} intercom_mode={is_intercom}"
        )
        try:
            resp = {
                "type": "intercom_ready",
                "status": "ok" if is_intercom else "no_session",
            }
            await conn.safe_send(json.dumps(resp, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            conn.logger.bind(tag=TAG).warning(f"[v3.0][intercom_ready] 回包失败: {e}")
