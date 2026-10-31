"""Abort（中止播报）上行消息处理器。

设备侧通过 ABORT 文本消息表达"停播/打断"意图。本处理器只负责把该意图翻译成对
`handleAbortMessage` 的调用：全双工 realtime 模式下，仅当信令明确为遗留语音
barge-in（reason=="barge_in"）时才忽略（改由后端 AEC 驱动）；其余显式手势一律
作为确定性打断处理（停 TTS + 发 tts stop + 清 ASR 缓冲 + 转聆听）。
"""

from typing import Any, Dict

from fuguoai_core.handle.abortHandle import handleAbortMessage
from fuguoai_core.handle.textMessageHandler import TextMessageHandler
from fuguoai_core.handle.textMessageType import TextMessageType


_LOG = __name__
# 遗留语音打断信令；realtime 下由后端 AEC 驱动，不再作显式手势处理
_LEGACY_VOICE = "barge_in"


class AbortTextMessageHandler(TextMessageHandler):
    """ABORT 消息处理器。"""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.ABORT

    async def handle(self, conn, packet: Dict[str, Any]) -> None:
        cause = packet.get("reason")
        # 显式手势 = 非遗留语音打断；realtime+遗留语音打断才忽略
        explicit = cause != _LEGACY_VOICE
        if self._ignore_as_legacy(conn, explicit):
            conn.logger.bind(tag=_LOG).info(
                "Abort(realtime): legacy voice barge-in 信令已忽略, TTS keeps playing"
                "（真人打断由后端 AEC 驱动）"
            )
            return
        # from_button 恒为"显式手势"(realtime+legacy 已在上方拦截，其余均非 legacy)
        await handleAbortMessage(conn, from_button=explicit)

    def _ignore_as_legacy(self, conn, explicit: bool) -> bool:
        mode = getattr(conn, "client_listen_mode", "")
        return mode == "realtime" and not explicit
