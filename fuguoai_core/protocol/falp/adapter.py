"""FALP adapter — fg.* 文本帧 / FALP 二进制帧 <-> canonical 领域事件。

本文件是 P1 原生层与内部业务链的接缝。legacy(xiaozhi) 路径完全不经过这里。
"""
from __future__ import annotations

from typing import Optional

from .canonical import (
    CanonicalAbort, CanonicalAsr, CanonicalAudio, CanonicalAuthResult,
    CanonicalHello, CanonicalIot, CanonicalLlm, CanonicalNfc, CanonicalSchedule,
    CanonicalTts,
)
from .envelope import FalpMessage


# ---------------- fg.* -> canonical ----------------
def hello_to_canonical(msg: FalpMessage) -> CanonicalHello:
    d = msg.data["device"]
    return CanonicalHello(
        device_id=d["id"], board=d["board"], fw=d["fw"],
        proto=msg.data.get("proto", "falp"), version=msg.data.get("version", 1))


def audio_frame_to_canonical(frame) -> CanonicalAudio:
    return CanonicalAudio(
        direction_up=frame.direction_up, session_id=frame.session_id,
        timestamp=frame.timestamp, opus=frame.payload)


# ---------------- canonical -> fg.* ----------------
def asr_from_canonical(c: CanonicalAsr, sid: Optional[str] = None) -> FalpMessage:
    return FalpMessage.asr(c.text, c.final, sid=sid)


def llm_from_canonical(c: CanonicalLlm, sid: Optional[str] = None) -> FalpMessage:
    return FalpMessage.llm(c.text, c.segment, sid=sid)


def tts_from_canonical(c: CanonicalTts, sid: Optional[str] = None) -> FalpMessage:
    return FalpMessage.tts(c.state, c.sentence_id, sid=sid)


def iot_from_canonical(c: CanonicalIot, sid: Optional[str] = None) -> FalpMessage:
    return FalpMessage.iot(commands=c.commands, event=c.event, sid=sid)


def abort_from_canonical(c: CanonicalAbort, sid: Optional[str] = None) -> FalpMessage:
    return FalpMessage.abort(c.reason, sid=sid)


def nfc_from_canonical(c: CanonicalNfc, sid: Optional[str] = None) -> FalpMessage:
    return FalpMessage.nfc(c.op, c.card_uid, sid=sid)


def schedule_from_canonical(c: CanonicalSchedule, sid: Optional[str] = None) -> FalpMessage:
    return FalpMessage.schedule(c.op, tasks=c.tasks, sid=sid)
