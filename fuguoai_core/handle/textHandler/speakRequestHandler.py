from typing import Dict, Any

from fuguoai_core.handle.textMessageHandler import TextMessageHandler
from fuguoai_core.handle.textMessageType import TextMessageType
from fuguoai_core.utils.networked_reminder_scheduler import speak_text_to_device

TAG = __name__

# 与 networked_reminder_store.MAX_TEXT 对齐，避免长文本压垮 TTS / 推流超时
MAX_SPEAK_TEXT = 200


class SpeakRequestTextMessageHandler(TextMessageHandler):
    """设备在线时主动请求服务端播报任意文本（提醒/播报内容，B 方案）。

    设备侧契约（由固件实现，本仓不碰）：
      - FireTask 触发且在线 → 发 {"type":"speak_request","text":<task.message>,"req_id":<可选>}
        并跳过本地 29 条预烤片段（在线播真实内容，不再受片段覆盖限制）。
      - 离线 → 照旧播 29 条预烤片段（本 handler 不会触发）。
    后端行为：收到即合成 TTS，经 vm-server 虚拟对端(SetIntercom start→[4B seq][opus]→stop)
    推流到设备，固件零改动（复用留言/联网提醒同一条通道）。
    健壮性建议（固件侧）：若发请求后 N 秒内未收到 vm-server 音频，回退本地 29 条片段。
    """

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.SPEAK_REQUEST

    async def handle(self, conn, msg_json: Dict[str, Any]) -> None:
        text = (msg_json.get("text") or "").strip()
        req_id = msg_json.get("req_id") or ""
        did = conn.device_id
        if not text:
            conn.logger.bind(tag=TAG).warning(
                f"[speak_request] 空文本,忽略 did={did} req={req_id}"
            )
            return
        if len(text) > MAX_SPEAK_TEXT:
            conn.logger.bind(tag=TAG).warning(
                f"[speak_request] 文本超长截断 {len(text)}>{MAX_SPEAK_TEXT} did={did}"
            )
            text = text[:MAX_SPEAK_TEXT]
        conn.logger.bind(tag=TAG).info(
            f"[speak_request] 收到 did={did} req={req_id} len={len(text)}，合成并推流"
        )
        ok = await speak_text_to_device(did, text, label=f"speak_request {req_id}")
        conn.logger.bind(tag=TAG).info(
            f"[speak_request] 推流完成 did={did} req={req_id} ok={ok}"
        )
