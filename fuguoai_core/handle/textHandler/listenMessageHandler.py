"""LISTEN（拾音）上行消息处理器。

设备侧通过 LISTEN 文本消息上报拾音状态变化：切换拾音模式、开始/结束收音、
以及本地检测出语音文本。文本可能来自三类来源——设备呼叫指令、唤醒词、
或普通对话语句。本模块把这些状态翻译成对连接对象的属性写入与外部流程
调用，是全双工对话在「设备侧拾音」这一侧的唯一入口。
"""

import asyncio
import time
import uuid
from typing import Any, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler

from fuguoai_core.utils.dialogue import Message
from fuguoai_core.providers.asr.dto.dto import InterfaceType
from fuguoai_core.handle.receiveAudioHandle import startToChat
from fuguoai_core.handle.reportHandle import enqueue_asr_report
from fuguoai_core.handle.sendAudioHandle import send_stt_message, send_tts_message
from fuguoai_core.handle.textMessageHandler import TextMessageHandler
from fuguoai_core.handle.textMessageType import TextMessageType
from fuguoai_core.utils.util import remove_punctuation_and_length
from fuguoai_core.providers.tts.dto.dto import ContentType, TTSMessageDTO, SentenceType


_TRACE = __name__
_CALL_TAG = "[device_call]"
_TTS_READY_TIMEOUT = 3.0
_DEFAULT_GREETING = "嘿，你好呀"


class ListenTextMessageHandler(TextMessageHandler):
    """LISTEN 消息处理器。"""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.LISTEN

    async def handle(self, conn: "ConnectionHandler", packet: Dict[str, Any]) -> None:
        # v3.0 单动作类型名 → 既有 state 语义（向后兼容）。
        # 固件发 {"type":"start_listening"} / {"type":"stop_listening"} 等价于
        # 旧 {"type":"listen","state":"start"/"stop"}。
        msg_type = packet.get("type")
        if msg_type == "start_listening":
            packet = {**packet, "state": "start"}
        elif msg_type == "stop_listening":
            packet = {**packet, "state": "stop"}
        self._sync_listen_mode(conn, packet)
        stage = packet.get("state")
        if stage == "start":
            await self._enter_recording(conn)
        elif stage == "stop":
            await self._leave_recording(conn)
        elif stage == "detect":
            await self._react_to_speech(conn, packet)

    # 模式协商：realtime 额外开启服务端 AEC（阶段1旁路，不打断）
    def _sync_listen_mode(self, conn: "ConnectionHandler", packet: Dict[str, Any]) -> None:
        requested = packet.get("mode")
        if requested is None:
            return
        conn.client_listen_mode = requested
        conn.logger.bind(tag=_TRACE).debug(f"客户端拾音模式：{conn.client_listen_mode}")
        if requested == "realtime":
            conn.client_aec = True

    async def _enter_recording(self, conn: "ConnectionHandler") -> None:
        conn.reset_audio_states()

    async def _leave_recording(self, conn: "ConnectionHandler") -> None:
        engine = conn.asr
        if engine is None:
            return
        conn.client_voice_stop = True
        if engine.interface_type == InterfaceType.STREAM:
            asyncio.create_task(engine._send_stop_request())
            return
        if conn.asr_audio:
            captured = conn.asr_audio.copy()
            conn.reset_audio_states()
            await engine.handle_voice_stop(conn, captured)

    async def _react_to_speech(self, conn: "ConnectionHandler", packet: Dict[str, Any]) -> None:
        conn.client_have_voice = False
        conn.reset_audio_states()
        spoken_raw = packet.get("text")
        if spoken_raw is None:
            return
        conn.last_activity_time = time.time() * 1000
        _, cleaned = remove_punctuation_and_length(spoken_raw)

        if spoken_raw.startswith(_CALL_TAG):
            await self._answer_device_call(conn, spoken_raw)
            return

        is_wake = cleaned in conn.config.get("wakeup_words")
        allow_greet = conn.config.get("enable_greeting", True)
        if is_wake and not allow_greet:
            await send_stt_message(conn, spoken_raw)
            await send_tts_message(conn, "stop", None)
            conn.client_is_speaking = False
            return

        conn.just_woken_up = True
        reply_text = conn.config.get("greeting_text", _DEFAULT_GREETING) if is_wake else spoken_raw
        enqueue_asr_report(conn, reply_text, [])
        await startToChat(conn, reply_text)

    async def _answer_device_call(self, conn: "ConnectionHandler", spoken_raw: str) -> None:
        call_body = spoken_raw[len(_CALL_TAG):].strip()
        conn.logger.bind(tag=_TRACE).info(f"收到设备呼叫指令: {call_body}")
        conn.incoming_call = True
        conn.sentence_id = uuid.uuid4().hex
        await send_stt_message(conn, call_body)
        if not await self._wait_tts_ready(conn):
            return
        self._enqueue_call_playback(conn, call_body)

    async def _wait_tts_ready(self, conn: "ConnectionHandler") -> bool:
        deadline = time.time() + _TTS_READY_TIMEOUT
        while time.time() < deadline:
            if conn.tts is not None:
                return True
            await asyncio.sleep(0.1)
        return conn.tts is not None

    def _enqueue_call_playback(self, conn: "ConnectionHandler", call_body: str) -> None:
        conn.tts.store_tts_text(conn.sentence_id, call_body)
        for step_type, with_audio in (
            (SentenceType.FIRST, True),
            (SentenceType.LAST, False),
        ):
            conn.tts.tts_text_queue.put(
                TTSMessageDTO(
                    sentence_id=conn.sentence_id,
                    sentence_type=step_type,
                    content_type=ContentType.ACTION,
                )
            )
            if with_audio:
                conn.tts.tts_one_sentence(conn, ContentType.TEXT, content_detail=call_body)
        conn.dialogue.put(Message(role="assistant", content=call_body))
