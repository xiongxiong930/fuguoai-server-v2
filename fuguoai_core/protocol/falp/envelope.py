"""FALP v1 文本帧信封（fg.* 消息）。纯 stdlib，零依赖。

线格式（FALP §6）：JSON 对象，通用字段 {type, ts, sid} + 各类型载荷。
本模块提供统一数据类 FalpMessage + 各消息工厂方法，以及严格的 from_dict 校验
（未知类型 / 缺 type 直接抛错，便于行为门断言）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

_MSG_TYPES = {
    "fg.hello", "fg.challenge", "fg.auth", "fg.welcome", "fg.deny",
    "fg.listen", "fg.asr", "fg.llm", "fg.tts", "fg.iot", "fg.abort",
    "fg.schedule", "fg.nfc", "fg.ping", "fg.pong", "fg.bye", "fg.error",
}


@dataclass
class FalpMessage:
    type: str
    ts: int = 0
    sid: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"type": self.type}
        if self.ts:
            d["ts"] = self.ts
        if self.sid is not None:
            d["sid"] = self.sid
        d.update(self.data)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "FalpMessage":
        if not isinstance(d, dict) or "type" not in d:
            raise ValueError("invalid FALP envelope: missing 'type'")
        if d["type"] not in _MSG_TYPES:
            raise ValueError(f"unknown FALP message type: {d['type']!r}")
        return cls(
            type=d["type"],
            ts=int(d.get("ts", 0)),
            sid=d.get("sid"),
            data={k: v for k, v in d.items() if k not in ("type", "ts", "sid")},
        )

    # ---------- 工厂方法 ----------
    @classmethod
    def hello(cls, device_id: str, board: str, fw: str,
              proto: str = "falp", version: int = 1) -> "FalpMessage":
        return cls("fg.hello", data={
            "proto": proto, "version": version,
            "device": {"id": device_id, "board": board, "fw": fw}})

    @classmethod
    def challenge(cls, nonce: str, expires_in: int = 30) -> "FalpMessage":
        return cls("fg.challenge", data={"nonce": nonce, "expires_in": expires_in})

    @classmethod
    def auth(cls, device_id: str, token: str) -> "FalpMessage":
        return cls("fg.auth", data={"device_id": device_id, "token": token})

    @classmethod
    def welcome(cls, sid: str, server_name: str = "fuguoai-server",
                server_version: str = "1.2.0",
                caps: Optional[Dict[str, Any]] = None) -> "FalpMessage":
        return cls("fg.welcome", sid=sid, data={
            "server": {"name": server_name, "version": server_version},
            "caps": caps or {"bargein": "auto", "listen_mode": "auto", "nfc": True}})

    @classmethod
    def deny(cls, code: str, reason: str) -> "FalpMessage":
        return cls("fg.deny", data={"code": code, "reason": reason})

    @classmethod
    def listen(cls, state: str) -> "FalpMessage":
        return cls("fg.listen", data={"state": state})

    @classmethod
    def asr(cls, text: str, final: bool, sid: Optional[str] = None) -> "FalpMessage":
        return cls("fg.asr", sid=sid, data={"text": text, "final": final})

    @classmethod
    def llm(cls, text: str, segment: bool, sid: Optional[str] = None) -> "FalpMessage":
        return cls("fg.llm", sid=sid, data={"text": text, "segment": segment})

    @classmethod
    def tts(cls, state: str, sentence_id: int = 0,
            sid: Optional[str] = None) -> "FalpMessage":
        return cls("fg.tts", sid=sid, data={"state": state, "sentence_id": sentence_id})

    @classmethod
    def iot(cls, commands: Optional[list] = None, event: Optional[dict] = None,
            sid: Optional[str] = None) -> "FalpMessage":
        data: Dict[str, Any] = {}
        if commands is not None:
            data["commands"] = commands
        if event is not None:
            data["event"] = event
        return cls("fg.iot", sid=sid, data=data)

    @classmethod
    def abort(cls, reason: str, sid: Optional[str] = None) -> "FalpMessage":
        return cls("fg.abort", sid=sid, data={"reason": reason})

    @classmethod
    def schedule(cls, op: str, tasks: Optional[list] = None,
                 sid: Optional[str] = None) -> "FalpMessage":
        data: Dict[str, Any] = {"op": op}
        if tasks is not None:
            data["tasks"] = tasks
        return cls("fg.schedule", sid=sid, data=data)

    @classmethod
    def nfc(cls, op: str, card_uid: str, sid: Optional[str] = None) -> "FalpMessage":
        return cls("fg.nfc", sid=sid, data={"op": op, "card_uid": card_uid})

    @classmethod
    def ping(cls, sid: Optional[str] = None) -> "FalpMessage":
        return cls("fg.ping", sid=sid, data={})

    @classmethod
    def pong(cls, sid: Optional[str] = None) -> "FalpMessage":
        return cls("fg.pong", sid=sid, data={})

    @classmethod
    def bye(cls, sid: Optional[str] = None) -> "FalpMessage":
        return cls("fg.bye", sid=sid, data={})

    @classmethod
    def error(cls, code: int, reason: str, sid: Optional[str] = None) -> "FalPMessage":
        return cls("fg.error", sid=sid, data={"code": code, "reason": reason})
