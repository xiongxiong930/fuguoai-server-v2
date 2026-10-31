"""设备侧双层 token 认证（clean-room 独立重写）。

外层 JWT(HS256) 保证完整性与签名；内层 payload 用 AES-256-GCM 加密，
使 device_id 与过期时间不直接暴露在 token 明文。

安全契约（与存量设备约定的硬契约，任何改动都会使已签发 token 全部失效）：
- 密钥派生：PBKDF2-HMAC-SHA256，salt = b"fixed_salt_placeholder"，
  iterations = 100000，派生长度 = 32 字节（AES-256 密钥）。
- 内层加密：AES-256-GCM，IV = 12 字节随机，GCM tag = 16 字节。
- 内层密文布局：base64urlsafe( IV(12) + ciphertext + tag(16) )。
- 外层 token：JWT HS256，payload = {"data": <内层 base64urlsafe 字符串>}。
- 内层明文 JSON：{"device_id": <str>, "exp": <unix 时间戳 int>}。
- TTL：timedelta(hours=1)（exp = now_utc + 1h）。
- 过期判定由内层 exp 把关（外层 JWT 不设 exp）。
"""

import base64
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

logger = logging.getLogger(__name__)

# 与存量设备约定的硬契约参数，禁止修改。
_PBKDF2_SALT = b"fixed_salt_placeholder"
_PBKDF2_ITERATIONS = 100000
_PBKDF2_DK_LEN = 32  # AES-256 密钥长度
_GCM_IV_LEN = 12
_GCM_TAG_LEN = 16
_TOKEN_TTL = timedelta(hours=1)


class AuthToken:
    """签发与校验双层设备 token。"""

    def __init__(self, secret_key: str):
        """使用共享密钥初始化。

        Args:
            secret_key: 用于 JWT(HS256) 签名的共享密钥字符串。
        """
        self._secret_key = secret_key
        # 从密钥字符串派生 AES-256 密钥（硬契约：PBKDF2-HMAC-SHA256）。
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=_PBKDF2_DK_LEN,
            salt=_PBKDF2_SALT,
            iterations=_PBKDF2_ITERATIONS,
        )
        self._aes_key = kdf.derive(secret_key.encode("utf-8"))
        self._aesgcm = AESGCM(self._aes_key)

    def generate_token(self, device_id: str) -> str:
        """签发一枚一小时后过期的设备 token。

        Args:
            device_id: 设备标识字符串。

        Returns:
            外层 JWT(HS256) 字符串。
        """
        now = datetime.now(timezone.utc)
        exp = int((now + _TOKEN_TTL).timestamp())
        inner_plaintext = json.dumps(
            {"device_id": device_id, "exp": exp},
            separators=(",", ":"),
        ).encode("utf-8")

        iv = os.urandom(_GCM_IV_LEN)
        ciphertext = self._aesgcm.encrypt(iv, inner_plaintext, None)
        # 内层密文布局：IV(12) + ciphertext + tag(16)
        inner_blob = iv + ciphertext
        inner_b64 = base64.urlsafe_b64encode(inner_blob).decode("ascii")

        token = jwt.encode({"data": inner_b64}, self._secret_key, algorithm="HS256")
        # PyJWT 新版返回 str，旧版返回 bytes，统一为 str。
        if isinstance(token, bytes):
            token = token.decode("utf-8")
        return token

    def verify_token(self, token: str) -> Tuple[bool, Optional[str]]:
        """校验 token，返回 (是否有效, device_id)。

        签名不符 / 已过期 / 结构异常 / 解密失败 均返回 (False, None)。
        本方法永不向外抛异常。

        Args:
            token: 待校验的 JWT 字符串。

        Returns:
            (True, device_id) 或 (False, None)。
        """
        try:
            outer = jwt.decode(token, self._secret_key, algorithms=["HS256"])
            inner_b64 = outer.get("data")
            if not isinstance(inner_b64, str):
                return False, None

            # urlsafe_b64encode 可能不带 '=' 填充，补齐后解码。
            padded = inner_b64
            if len(padded) % 4 != 0:
                padded += "=" * (4 - len(padded) % 4)
            inner_blob = base64.urlsafe_b64decode(padded)
            iv = inner_blob[:_GCM_IV_LEN]
            ciphertext = inner_blob[_GCM_IV_LEN:]
            plaintext = self._aesgcm.decrypt(iv, ciphertext, None)

            inner = json.loads(plaintext.decode("utf-8"))
            exp = inner.get("exp")
            device_id = inner.get("device_id")
            if not isinstance(exp, (int, float)) or not isinstance(device_id, str):
                return False, None

            # 过期判定由内层 exp 把关。
            if exp < int(time.time()):
                return False, None

            return True, device_id
        except Exception:  # 统一归并为无效。
            logger.debug("verify_token failed", exc_info=True)
            return False, None
