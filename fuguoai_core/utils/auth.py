"""设备侧双层 token（复果自有实现）。

外层 JWT(HS256) 保证完整性与签名，内层 payload 用 AES-GCM 加密，
使 device_id 与过期时间不直接暴露在 token 明文中。

token 结构：
    JWT({"data": base64url(iv(12B) + ciphertext + GCM tag(16B))})
    内层明文 = {"device_id": <id>, "exp": <unix 时间戳>}

兼容性红线：密钥派生盐值与迭代次数、密文拼接布局均为存量设备契约，
改动会使已签发 token 全部失效，如需调整须配合全量重签。
"""

import base64
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import jwt
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

_IV_BYTES = 12        # GCM 推荐 IV 长度
_TAG_BYTES = 16       # GCM tag 长度
_KEY_BYTES = 32       # AES-256
_DERIVE_ITERATIONS = 100000
# 密钥派生盐值：存量契约，不可变更（改则存量 token 失效）
_DERIVE_SALT = b"fixed_salt_placeholder"
_TOKEN_TTL = timedelta(hours=1)


class AuthToken:
    """设备 token 的签发与校验（JWT 外层 + AES-GCM 内层）。"""

    def __init__(self, secret_key: str):
        self.secret_key = secret_key.encode()
        self.encryption_key = self._derive_key(_KEY_BYTES)

    def _derive_key(self, length: int) -> bytes:
        """由主密钥派生定长加密密钥（PBKDF2-HMAC-SHA256）。"""
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=length,
            salt=_DERIVE_SALT,
            iterations=_DERIVE_ITERATIONS,
            backend=default_backend(),
        )
        return kdf.derive(self.secret_key)

    def _encrypt_payload(self, payload: dict) -> str:
        """加密 payload，输出 base64url(iv + 密文 + tag)。"""
        iv = os.urandom(_IV_BYTES)
        encryptor = Cipher(
            algorithms.AES(self.encryption_key),
            modes.GCM(iv),
            backend=default_backend(),
        ).encryptor()
        ciphertext = encryptor.update(json.dumps(payload).encode()) + encryptor.finalize()
        return base64.urlsafe_b64encode(iv + ciphertext + encryptor.tag).decode()

    def _decrypt_payload(self, encrypted_data: str) -> dict:
        """还原 _encrypt_payload 的输出。"""
        blob = base64.urlsafe_b64decode(encrypted_data.encode())
        iv = blob[:_IV_BYTES]
        tag = blob[-_TAG_BYTES:]
        ciphertext = blob[_IV_BYTES:-_TAG_BYTES]

        decryptor = Cipher(
            algorithms.AES(self.encryption_key),
            modes.GCM(iv, tag),
            backend=default_backend(),
        ).decryptor()
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        return json.loads(plaintext.decode())

    def generate_token(self, device_id: str) -> str:
        """签发一枚一小时后过期的设备 token。"""
        payload = {
            "device_id": device_id,
            "exp": (datetime.now(timezone.utc) + _TOKEN_TTL).timestamp(),
        }
        return jwt.encode(
            {"data": self._encrypt_payload(payload)},
            self.secret_key,
            algorithm="HS256",
        )

    def verify_token(self, token: str) -> Tuple[bool, Optional[str]]:
        """校验 token，返回 (是否有效, device_id)。

        签名不符、已过期或结构异常均返回 (False, None)。
        """
        try:
            outer_payload = jwt.decode(token, self.secret_key, algorithms=["HS256"])
            inner_payload = self._decrypt_payload(outer_payload["data"])
            # 外层 JWT 不设 exp，过期由内层时间戳把关
            if inner_payload["exp"] < time.time():
                return False, None
            return True, inner_payload["device_id"]
        except jwt.InvalidTokenError:
            return False, None
        except json.JSONDecodeError:
            return False, None
        except Exception as exc:
            logger.warning("设备 token 校验失败: %s", exc)
            return False, None
