"""FALP 内部领域事件（canonical）——与线格式无关，adapter 的桥。

后端业务逻辑只认 canonical 事件；fg.* 文本帧 / FALP 二进制帧由 adapter 在其间转换。
这样 P1 原生层与旧 xiaozhi 协议栈彻底解耦：业务处理链复用，仅换协议壳。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class CanonicalHello:
    device_id: str
    board: str
    fw: str
    proto: str = "falp"
    version: int = 1


@dataclass
class CanonicalAuthResult:
    device_id: str
    nonce: str
    ok: bool
    session_id: Optional[str] = None


@dataclass
class CanonicalAudio:
    direction_up: bool
    session_id: int
    timestamp: int
    opus: bytes


@dataclass
class CanonicalAsr:
    text: str
    final: bool


@dataclass
class CanonicalLlm:
    text: str
    segment: bool


@dataclass
class CanonicalTts:
    state: str
    sentence_id: int = 0


@dataclass
class CanonicalIot:
    commands: Optional[List[Dict[str, Any]]] = None
    event: Optional[Dict[str, Any]] = None


@dataclass
class CanonicalAbort:
    reason: str


@dataclass
class CanonicalNfc:
    op: str
    card_uid: str


@dataclass
class CanonicalSchedule:
    op: str
    tasks: Optional[List[Dict[str, Any]]] = None
