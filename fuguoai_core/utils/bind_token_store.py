"""配网绑定令牌存储（bind_token）（自研重构版）。

绑定流程（三方）
----------------
1. 小程序（已登录，持 JWT）调 `POST /api/v1/devices/bind-token` 领取一次性短令牌 bind_token（含 TTL）。
2. 小程序经 BLE 把 wifi 凭据 + bind_token 下发给 ESP32（固件 token 缓冲仅 64B，故必须是短串，不能塞 JWT）。
3. ESP32 联网后 `POST /api/v1/devices/bind {device_id, bind_token}`，后端凭 token 反查 user_id，
   写 device_binding(device_id, user_id, status='active')。

存储：`data/device_agent.db` 的 `device_bind_tokens` 表（与 device_binding / agent_config 同库，同属设备域）。
令牌一次性：绑定成功即标记 used。重复用同一 token 绑「同一」device 幂等放行；绑「不同」device 拒绝。

相对 fork 起点的改进（契约不变）：
- 每线程连接缓存 + WAL（并发更安全，避免每次调用 open/close）。
- 读写异常分级日志（error 用于改变状态的操作，warning 用于只读降级）。
- 公开 API（类名 / 方法签名 / SQL schema）完全兼容调用方与线上库文件。
"""
import logging
import os
import secrets
import sqlite3
import threading
import time

from fuguoai_config.config_loader import get_project_dir

TAG = "bind_token_store"
logger = logging.getLogger(TAG)

# 每线程一把连接（配合 WAL，多线程读写安全）
_thread_local = threading.local()

_INSTANCE = None


def get_bind_token_store() -> "BindTokenStore":
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = BindTokenStore()
    return _INSTANCE


class BindTokenStore:
    """一次性配网绑定令牌。表缺失自动建表，读写异常降级，绝不抛断连接。"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or (get_project_dir() + "data/device_agent.db")
        d = os.path.dirname(self.db_path)
        if d:
            os.makedirs(d, exist_ok=True)
        # 触发首连建表
        self._connect()

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(_thread_local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=5, check_same_thread=False)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.Error:
                pass
            _thread_local.conn = conn
            self._ensure(conn)
        return conn

    def _ensure(self, conn: sqlite3.Connection) -> None:
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS device_bind_tokens ("
                "token TEXT PRIMARY KEY, user_id TEXT, created_at INTEGER, "
                "expires_at INTEGER, used_at INTEGER, device_id TEXT)"
            )
            conn.commit()
        except sqlite3.Error as e:
            logger.error(f"建表失败: {e}")

    def issue(self, user_id: str, ttl: int = 600) -> dict:
        """签发一次性 bind_token。返回 {bind_token, expires_at, expires_in}。"""
        token = secrets.token_hex(16)  # 32 hex 字符，稳落在固件 token[64] 内
        now = int(time.time())
        try:
            conn = self._connect()
            conn.execute(
                "INSERT INTO device_bind_tokens("
                "token, user_id, created_at, expires_at, used_at, device_id) "
                "VALUES(?, ?, ?, ?, NULL, NULL)",
                (token, user_id, now, now + ttl),
            )
            conn.commit()
        except sqlite3.Error as e:
            logger.error(f"签发 bind_token 失败: {e}")
            raise
        return {"bind_token": token, "expires_at": now + ttl, "expires_in": ttl}

    def get(self, token: str):
        """返回令牌记录 dict 或 None。"""
        if not token:
            return None
        try:
            conn = self._connect()
            r = conn.execute(
                "SELECT token, user_id, created_at, expires_at, used_at, device_id "
                "FROM device_bind_tokens WHERE token=?",
                (token,),
            ).fetchone()
        except sqlite3.Error as e:
            logger.warning(f"读取 bind_token 失败: {e}")
            return None
        if not r:
            return None
        return {
            "token": r[0],
            "user_id": r[1],
            "created_at": r[2],
            "expires_at": r[3],
            "used_at": r[4],
            "device_id": r[5],
        }

    def mark_used(self, token: str, device_id: str) -> None:
        """绑定成功后标记令牌已用并记录目标 device_id。"""
        try:
            conn = self._connect()
            conn.execute(
                "UPDATE device_bind_tokens SET used_at=?, device_id=? WHERE token=?",
                (int(time.time()), device_id, token),
            )
            conn.commit()
        except sqlite3.Error as e:
            logger.error(f"标记 bind_token 已用失败: {e}")
