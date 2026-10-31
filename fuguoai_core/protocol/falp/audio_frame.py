"""FALP v1 二进制音频帧（FALP §7）。纯 stdlib，零依赖。

帧结构（18 字节头 + opus payload）：
  magic 'FALP'(4) | ver(1) | flags(1) | session_id(uint32) | timestamp(uint32) | payload_len(uint32) | payload
flags: bit0 = 方向(0 上行 / 1 下行)；bit1 = 末帧(一句话最后一片)。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC = b"FALP"
_HEADER = struct.Struct("<4sBBIII")  # magic, ver, flags, session_id, timestamp, payload_len
HEADER_SIZE = _HEADER.size  # 4+1+1+4+4+4 = 18


@dataclass
class FalpAudioFrame:
    session_id: int
    timestamp: int
    direction_up: bool
    is_last: bool
    payload: bytes

    def encode(self) -> bytes:
        flags = (0 if self.direction_up else 1) | (2 if self.is_last else 0)
        return _HEADER.pack(
            MAGIC, 1, flags,
            self.session_id & 0xFFFFFFFF,
            self.timestamp & 0xFFFFFFFF,
            len(self.payload),
        ) + self.payload

    @classmethod
    def decode(cls, buf: bytes) -> "FalpAudioFrame":
        if len(buf) < HEADER_SIZE:
            raise ValueError(f"FALP frame too short: {len(buf)} < {HEADER_SIZE}")
        magic, ver, flags, sid, ts, plen = _HEADER.unpack(buf[:HEADER_SIZE])
        if magic != MAGIC:
            raise ValueError("bad FALP magic")
        if ver != 1:
            raise ValueError(f"unsupported FALP version {ver}")
        payload = buf[HEADER_SIZE:HEADER_SIZE + plen]
        if len(payload) != plen:
            raise ValueError("FALP payload length mismatch")
        return cls(
            session_id=sid,
            timestamp=ts,
            direction_up=(flags & 1) == 0,
            is_last=bool(flags & 2),
            payload=payload,
        )
