"""设备接入鉴权模块（clean-room 独立重写）。

基于行为规格书原创实现，仅依赖 Python 标准库（base64 / hashlib / hmac / time）。
提供 HMAC-SHA256 签名 + 时间戳三元组的可过期 token 生成与校验能力。

设备侧硬契约（不可更改，否则存量设备鉴权失败）：
- token 格式： "<signature>.<timestamp>"（单个 "." 连接）。
- 签名输入： "{client_id}|{username}|{issued_at}"（竖线分隔，顺序固定）。
- 编码：HMAC-SHA256(密钥 utf-8) → urlsafe base64，并去掉尾部 "=" 填充。
"""

import base64
import hashlib
import hmac
import time

# 默认有效期：30 天
DEFAULT_EXPIRE_SECONDS = 60 * 60 * 24 * 30


class AuthenticationError(Exception):
    """设备鉴权失败（token 无效 / 缺失 / 未绑定设备）。

    注意：本异常不在 AuthManager 内部抛出，仅供调用方（如 connection.py）
    通过 ``from fuguoai_core.auth import AuthenticationError`` 引用。
    """


class AuthManager:
    """基于 HMAC-SHA256 的设备 token 鉴权器。

    可用于设备 WebSocket / MQTT 接入鉴权。token 本身不携带明文
    client_id / username（连接时另行传递），仅含签名与时间戳。
    """

    def __init__(self, secret_key: str, expire_seconds: int = DEFAULT_EXPIRE_SECONDS):
        """初始化鉴权器。

        参数：
            secret_key: 用于 HMAC 签名的共享密钥。
            expire_seconds: token 有效期（秒）。若非正或缺失，回落为默认 30 天。
        """
        self._secret_key = secret_key.encode("utf-8")
        # 非正的过期时间一律回落为默认有效期（契约 B6）
        if not isinstance(expire_seconds, int) or expire_seconds <= 0:
            expire_seconds = DEFAULT_EXPIRE_SECONDS
        self.expire_seconds = expire_seconds

    def _sign(self, content: str) -> str:
        """对给定明文计算 HMAC-SHA256 签名（urlsafe base64，去 '=' 填充）。

        参数：
            content: 待签名的完整明文（调用方负责拼接三元组）。
        返回：
            不含 "=" 填充的 urlsafe base64 签名串。
        """
        digest = hmac.new(
            self._secret_key, content.encode("utf-8"), hashlib.sha256
        ).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def generate_token(self, client_id: str, username: str) -> str:
        """为指定客户端与用户生成一次性设备接入 token。

        参数：
            client_id: 客户端标识。
            username: 用户名。
        返回：
            "<signature>.<issued_at>" 形式的 token 字符串。
        """
        issued_at = int(time.time())
        payload = f"{client_id}|{username}|{issued_at}"
        signature = self._sign(payload)
        return f"{signature}.{issued_at}"

    def verify_token(self, token: str, client_id: str, username: str) -> bool:
        """校验 token 是否对当前 (client_id, username) 有效且未过期。

        任何解析 / 比对异常一律归并为 False，不向上抛出（契约 B5）。

        参数：
            token: generate_token 产出的 token 字符串。
            client_id: 期望的客户端标识。
            username: 期望的用户名。
        返回：
            校验通过且未过期为 True，否则 False。
        """
        try:
            if not isinstance(token, str) or "." not in token:
                return False
            signature_part, timestamp_part = token.split(".", 1)
            issued_at = int(timestamp_part)
            current = int(time.time())
            if current - issued_at > self.expire_seconds:
                return False
            expected_payload = f"{client_id}|{username}|{issued_at}"
            expected_signature = self._sign(expected_payload)
            return hmac.compare_digest(signature_part, expected_signature)
        except (ValueError, TypeError, AttributeError):
            return False
