import json
from typing import Dict, Any, Optional, TYPE_CHECKING

from fuguoai_config.logger import setup_logging

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler
from fuguoai_core.handle.textMessageHandler import TextMessageHandler
from fuguoai_core.handle.textMessageType import TextMessageType

TAG = __name__
logger = setup_logging()


class NfcClaimTextMessageHandler(TextMessageHandler):
    """天使心：盒子 reader 经设备 WS 上报领卡（一次性绑 owner，模型 b）。

    与 nfc_tap 同链路：设备读卡只上报只读 card_uid，后端按 card_registry 裁决。
    已领 → 回包 ok=false reason=already_claimed（409 语义）；未知卡 → unknown_card（404 语义）。
    设备须已绑定到用户，否则无法归属 owner。
    """

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.NFC_CLAIM

    async def handle(self, conn: "ConnectionHandler", msg_json: Dict[str, Any]) -> None:
        card_uid = (msg_json.get("card_uid") or "").strip()
        device_id = getattr(conn, "device_id", None)
        if not card_uid:
            await self._ack(conn, ok=False, reason="card_uid 必填")
            return
        uid = self._resolve_uid(device_id)
        if not uid:
            logger.bind(tag=TAG).warning(
                f"[nfc_claim] 设备未绑定用户 did={device_id}，拒绝领卡"
            )
            await self._ack(conn, ok=False, reason="device_not_bound")
            return

        # 复用 HTTP 入口同一套核心逻辑
        from fuguoai_core.api.character_routes import _process_nfc_claim

        status, payload = await _process_nfc_claim(uid, card_uid)
        await self._ack(conn, ok=(status == 200), reason=(None if status == 200 else payload),
                        payload=(payload if status == 200 else None))

    @staticmethod
    async def _ack(conn: "ConnectionHandler", ok: bool, reason: Optional[str] = None,
                   payload: Optional[dict] = None) -> None:
        ack = {"type": "nfc_claim", "ok": ok}
        if ok and payload:
            ack.update(payload)
        if not ok and reason:
            ack["reason"] = reason
        try:
            await conn.safe_send(json.dumps(ack, ensure_ascii=False))
        except Exception as e:
            logger.bind(tag=TAG).error(f"[nfc_claim] 回包失败: {e}")

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
            logger.bind(tag=TAG).error(f"[nfc_claim] 反查 uid 失败: {e}")
        return None
