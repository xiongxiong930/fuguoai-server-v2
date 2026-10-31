"""设备接入鉴权（复果自有实现）。

设备侧 WebSocket / MQTT 采用"HMAC-SHA256 签名 + 时间戳"三元组鉴权：

    token      = "<signature>.<timestamp>"
    signature  = base64url(HMAC-SHA256(secret_key, "<client_id>|<username>|<timestamp>"))

token 自身不携带明文 client_id / device_id，二者在连接时另行传递：
  * MQTT      : client_id=client_id, username=device_id, password=token
  * WebSocket : header 携带 Device-ID / Client-ID / Authorization: Bearer <token>

注意：token 结构与签名输入顺序是设备侧契约，任何调整都会让存量设备鉴权失败。
"""

import base64
import hashlib
import hmac
import time

# 默认有效期 30 天
DEFAULT_EXPIRE_SECONDS = 60 * 60 * 24 * 30


class AuthenticationError(Exception):
    """设备鉴权失败（token 无效 / 缺失 / 未绑定设备）。"""


class AuthManager:
    """设备 token 的签发与校验。"""

    def __init__(self, secret_key: str, expire_seconds: int = DEFAULT_EXPIRE_SECONDS):
        """Args:
            secret_key: 服务端密钥。
            expire_seconds: token 有效期（秒）；非正或缺失时回落为默认 30 天。
        """
        self.secret_key = secret_key
        self.expire_seconds = (
            expire_seconds
            if expire_seconds and expire_seconds > 0
            else DEFAULT_EXPIRE_SECONDS
        )

    def _sign(self, content: str) -> str:
        """对给定内容做 HMAC-SHA256，返回去掉 padding 的 urlsafe base64。"""
        digest = hmac.new(
            self.secret_key.encode("utf-8"),
            content.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")

    def generate_token(self, client_id: str, username: str) -> str:
        """为 (client_id, username) 签发 token。

        Args:
            client_id: 设备连接 ID。
            username: 设备用户名（通常为 device_id）。
        """
        issued_at = int(time.time())
        signature = self._sign(f"{client_id}|{username}|{issued_at}")
        return f"{signature}.{issued_at}"

    def verify_token(self, token: str, client_id: str, username: str) -> bool:
        """校验 token 是否由本密钥签发且未过期。

        任何解析/比对异常一律判为无效，不向上抛出。
        """
        try:
            signature, issued_at_str = token.split(".")
            issued_at = int(issued_at_str)
        except Exception:
            return False

        if int(time.time()) - issued_at > self.expire_seconds:
            return False

        try:
            return hmac.compare_digest(
                signature, self._sign(f"{client_id}|{username}|{issued_at}")
            )
        except Exception:
            return False
