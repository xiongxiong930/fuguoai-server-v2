"""FALP-Auth v1 — HMAC-SHA256 挑战-响应鉴权（FALP §4）。纯 stdlib，零依赖。

设计要点（复果自有，非 xiaozhi auth_key 明文方案）：
  - 设备凭据 device_secret 每台唯一（出厂烧录 / 配置）。
  - 握手：服务端下发 nonce → 设备 token = HMAC-SHA256(device_secret, device_id || nonce)。
  - 防重放：nonce 单次使用（verify 时 pop）；带 expires_in 时限。
  - 本模块为纯状态机，不碰 IO / 配置；device_secret 由调用方注入（生产从 data/.config.yaml 取）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from typing import Dict, Optional, Tuple


class HandshakeError(Exception):
    pass


def _b64_nonce() -> str:
    return base64.b64encode(secrets.token_bytes(16)).decode("ascii")


def compute_token(device_secret: str, device_id: str, nonce: str) -> str:
    msg = f"{device_id}{nonce}".encode("utf-8")
    return hmac.new(device_secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


class HandshakeAuthority:
    """服务端侧：签发挑战 + 校验应答。nonce 单次使用即作废（防重放）。"""

    def __init__(self, expires_in: int = 30):
        self.expires_in = expires_in
        self._issued: Dict[str, int] = {}

    def issue(self) -> Tuple[str, int]:
        nonce = _b64_nonce()
        expires_at = int(time.time()) + self.expires_in
        self._issued[nonce] = expires_at
        return nonce, expires_at

    def verify(self, device_id: str, token: str, nonce: str,
               device_secret: str, now: Optional[int] = None) -> bool:
        now = int(time.time()) if now is None else now
        expires_at = self._issued.pop(nonce, None)  # 单次使用 -> 重放直接失败
        if expires_at is None:
            return False  # 未知 nonce 或已用过
        if now > expires_at:
            return False  # 过期
        expected = compute_token(device_secret, device_id, nonce)
        return hmac.compare_digest(expected, token)
