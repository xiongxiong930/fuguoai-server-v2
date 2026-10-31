import json
from typing import Dict, Any, Optional, TYPE_CHECKING

from fuguoai_config.logger import setup_logging

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler
from fuguoai_core.handle.textMessageHandler import TextMessageHandler
from fuguoai_core.handle.textMessageType import TextMessageType
from fuguoai_core.utils.character_store import CHANNEL_DEVICE

TAG = __name__
logger = setup_logging()


class NfcTapTextMessageHandler(TextMessageHandler):
    """天使心：盒子 reader 经设备 WS 上报碰卡（source=device，模型 b）。

    设备读卡只上报只读 card_uid（物理层读出，绝不经客户端），后端按 card_registry
    裁决 character_id；未知卡在后端直接硬拒，前端/设备无从伪造。设备须已绑定到用户，
    否则无法归属激活记录（盒子是用户自有设备，必已绑定）。
    """

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.NFC_TAP

    async def handle(self, conn: "ConnectionHandler", msg_json: Dict[str, Any]) -> None:
        card_uid = (msg_json.get("card_uid") or "").strip()
        device_id = getattr(conn, "device_id", None)
        if not card_uid:
            await self._ack(conn, ok=False, reason="card_uid 必填")
            return
        # 设备已绑定到用户：经 device_id 反查 uid 作为激活归属
        uid = self._resolve_uid(device_id)
        if not uid:
            logger.bind(tag=TAG).warning(
                f"[nfc_tap] 设备未绑定用户 did={device_id}，拒绝碰卡"
            )
            await self._ack(conn, ok=False, reason="device_not_bound")
            return

        # 复用 HTTP 入口同一套核心逻辑（模型 b、注入 device 面 system_prompt 一致）
        from fuguoai_core.api.character_routes import _process_nfc_tap

        status, payload = await _process_nfc_tap(
            uid, card_uid, device_id=device_id, source=CHANNEL_DEVICE
        )
        await self._ack(conn, ok=(status == 200), reason=(None if status == 200 else payload),
                        payload=(payload if status == 200 else None))

    @staticmethod
    async def _ack(conn: "ConnectionHandler", ok: bool, reason: Optional[str] = None,
                   payload: Optional[dict] = None) -> None:
        ack = {"type": "nfc_tap", "ok": ok}
        if ok and payload:
            ack.update(payload)
        if not ok and reason:
            ack["reason"] = reason
        try:
            await conn.safe_send(json.dumps(ack, ensure_ascii=False))
        except Exception as e:
            logger.bind(tag=TAG).error(f"[nfc_tap] 回包失败: {e}")

    @staticmethod
    def _resolve_uid(device_id: Optional[str]) -> Optional[str]:
        """经 device_id → user_id（设备绑定的微信 uid）。"""
        if not device_id:
            return None
        try:
            from fuguoai_core.device_access import DeviceBindingStore
            from fuguoai_core.utils.util import normalize_device_id

            did = normalize_device_id(device_id)
            for d in DeviceBindingStore().list_all():
                if normalize_device_id(d.get("device_id", "")) == did:
                    return d.get("user_id")
        except Exception as e:
            logger.bind(tag=TAG).error(f"[nfc_tap] 反查 uid 失败: {e}")
        return None
