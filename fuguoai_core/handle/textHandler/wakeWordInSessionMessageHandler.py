import time
from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler

from fuguoai_core.handle.textMessageHandler import TextMessageHandler
from fuguoai_core.handle.textMessageType import TextMessageType
from fuguoai_core.providers.tts.dto.dto import TTSMessageDTO, SentenceType, ContentType

TAG = __name__


class WakeWordInSessionTextMessageHandler(TextMessageHandler):
    """处理固件在 realtime 会话 Listening 态命中唤醒词时上行的 wake_word_in_session 信令。

    契约见 fuguoai-ble-firmware/src/WAKE_WORD_IN_SESSION_COORDINATION.md：
    固件只在 Realtime + Listening 态（无 TTS 回声源，WakeNet 命中=100% 真用户）发射，
    由后端定义语义。本实现采用契约第 4 节「方案 A：轻量确认」——
    回一句简短 TTS（"我在，请讲"），**不改变对话上下文**（不写 dialogue 历史、不走 LLM）。

    向后兼容：后端未注册该 type 时，固件 v1.6 不影响既有 realtime 链路（按未知消息忽略）。
    """

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.WAKE_WORD_IN_SESSION

    async def handle(self, conn: "ConnectionHandler", msg_json: Dict[str, Any]) -> None:
        word = msg_json.get("word") or "小美同学"
        session_id = msg_json.get("session_id")

        # ── 去重冷却：挡掉 TTS 回声环回导致的连续 wake_word_in_session ──
        # 真用户连续喊词间隔通常 >3s；回声环回在 AI 播完后数百 ms 内到达，会被挡掉，
        # 从而切断「播确认→回声→再唤醒→再播」的自聊死循环。
        _now = time.time()
        _last = getattr(conn, "_wake_in_session_last_ts", 0.0)
        if _now - _last < 3.0:
            conn.logger.bind(tag=TAG).info(
                f"[wake_word_in_session] 3s 内重复命中(word={word})，跳过确认播报"
                f"（去重冷却，挡回声环回）"
            )
            return
        conn._wake_in_session_last_ts = _now

        conn.logger.bind(tag=TAG).info(
            f"[wake_word_in_session] 会话内唤醒词命中: word={word} session={session_id}; "
            f"方案A=轻量确认(不改上下文)"
        )

        # 方案A：回一句简短TTS确认，让设备"有反应"，但不写对话历史、不走 LLM。
        # 复刻 intentHandler.speak_txt，但去掉 conn.dialogue.put(...) 以保持上下文不变。
        try:
            text = "我在，请讲"
            conn.tts.store_tts_text(conn.sentence_id, text)
            conn.tts.tts_text_queue.put(
                TTSMessageDTO(
                    sentence_id=conn.sentence_id,
                    sentence_type=SentenceType.FIRST,
                    content_type=ContentType.ACTION,
                )
            )
            conn.tts.tts_one_sentence(conn, ContentType.TEXT, content_detail=text)
            conn.tts.tts_text_queue.put(
                TTSMessageDTO(
                    sentence_id=conn.sentence_id,
                    sentence_type=SentenceType.LAST,
                    content_type=ContentType.ACTION,
                )
            )
        except Exception as e:
            conn.logger.bind(tag=TAG).warning(f"[wake_word_in_session] 确认播报失败: {e}")
