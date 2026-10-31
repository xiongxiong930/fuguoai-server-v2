"""设备隔离门禁 + 设备绑定持久层（clean-room 独立实现）。

仅依赖标准库（logging / os / sqlite3 / time / hmac / secrets）
外加 fuguoai_config.config_loader.get_project_dir 用于默认 db 路径。
本模块不依赖任何 fuguoai_core 内部模块。

行为契约见 cleanroom/device_access_spec.md。
"""

import logging
import os
import sqlite3
import time
import hmac
import secrets

from fuguoai_config.config_loader import get_project_dir

# ---------------------------------------------------------------------------
# 公开常量
# ---------------------------------------------------------------------------
POLICY_ALLOW_ALL = "allow_all"
POLICY_GUEST_ALLOWED = "guest_allowed"
POLICY_REQUIRE_ACTIVATION = "require_activation"

logger = logging.getLogger(__name__)

# 7 列版建表（兼容老表，无 device_token） —— 用于 _refresh 首次自动建表
_CREATE_TABLE_7 = (
    "CREATE TABLE IF NOT EXISTS device_binding ("
    "device_id TEXT PRIMARY KEY, user_id TEXT, device_name TEXT, "
    "activated_at INTEGER, status TEXT, online INTEGER DEFAULT 0, "
    "last_seen INTEGER DEFAULT 0)"
)

# 8 列版规范建表（含 device_token） —— 用于新表直接带列
_CREATE_TABLE_8 = (
    "CREATE TABLE IF NOT EXISTS device_binding ("
    "device_id TEXT PRIMARY KEY, user_id TEXT, device_name TEXT, "
    "activated_at INTEGER, status TEXT, device_token TEXT, "
    "online INTEGER DEFAULT 0, last_seen INTEGER DEFAULT 0)"
)


class DeviceBindingStore:
    """设备绑定持久层：sqlite 存储 + 内存状态缓存（带 TTL）。"""

    def __init__(self, db_path=None, cache_ttl=60):
        if db_path is None:
            db_path = get_project_dir() + "data/device_agent.db"
        self.db_path = db_path
        self._cache_ttl = cache_ttl
        self._cache = {}            # device_id -> status
        self._cache_ts = 0.0        # 上次刷新时间戳

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _connect(self):
        return sqlite3.connect(self.db_path, timeout=2)

    def _ensure_columns(self):
        """幂等确保表存在且含 device_token 列。

        - 新表：8 列 CREATE 直接带 device_token；
        - 老表（缺列）：ALTER TABLE ADD COLUMN device_token；
        - 其它 CREATE（7 列版）也保持幂等（CREATE TABLE IF NOT EXISTS）。
        异常必须被吞掉，绝不向外强抛。
        """
        try:
            with self._connect() as conn:
                conn.execute(_CREATE_TABLE_8)
        except sqlite3.OperationalError:
            # 表已存在且 schema 不一致等极端情况，交给下方 ALTER 兜底
            pass
        try:
            with self._connect() as conn:
                conn.execute(
                    "ALTER TABLE device_binding ADD COLUMN device_token TEXT"
                )
        except sqlite3.OperationalError:
            # 列已存在（或表缺失且上方 CREATE 已建好 8 列），忽略
            pass

    def _ensure_table(self):
        """幂等确保 device_binding 表存在（7 列版，用于非 device_token 操作）。"""
        try:
            with self._connect() as conn:
                conn.execute(_CREATE_TABLE_7)
        except sqlite3.OperationalError:
            pass

    def _refresh(self):
        """重建内存缓存（device_id -> status）。首次自动建表（7 列版）。"""
        try:
            with self._connect() as conn:
                conn.execute(_CREATE_TABLE_7)
                cur = conn.cursor()
                cur.execute("SELECT device_id, status FROM device_binding")
                rows = cur.fetchall()
            new_cache = {}
            for device_id, status in rows:
                new_cache[device_id] = status
            self._cache = new_cache
            self._cache_ts = time.time()
        except Exception as e:  # noqa: BLE001 - 降级而非强抛
            logger.warning("device_binding refresh failed: %s", e)
            self._cache = {}

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------
    def get_status(self, device_id):
        if not device_id:
            return None
        if time.time() - self._cache_ts > self._cache_ttl:
            self._refresh()
        return self._cache.get(device_id)

    def is_available(self):
        """以"表内是否有数据"判定是否落库。db 文件不存在或为异常 -> False。"""
        if not os.path.exists(self.db_path):
            return False
        try:
            with self._connect() as conn:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM device_binding")
                count = cur.fetchone()[0]
            return count > 0
        except Exception as e:  # noqa: BLE001
            logger.warning("device_binding is_available check failed: %s", e)
            return False

    def set_status(self, device_id, status, user_id=None):
        if not device_id:
            return
        try:
            self._ensure_table()
            with self._connect() as conn:
                if user_id is not None:
                    conn.execute(
                        "INSERT INTO device_binding(device_id,user_id,status) "
                        "VALUES(?,?,?) "
                        "ON CONFLICT(device_id) DO UPDATE SET "
                        "user_id=excluded.user_id, status=excluded.status",
                        (device_id, user_id, status))
                else:
                    conn.execute(
                        "INSERT INTO device_binding(device_id,status) "
                        "VALUES(?,?) "
                        "ON CONFLICT(device_id) DO UPDATE SET "
                        "status=excluded.status",
                        (device_id, status))
            self._refresh()
        except Exception as e:  # noqa: BLE001
            logger.warning("set_status failed for %s: %s", device_id, e)

    def list_all(self):
        try:
            self._ensure_table()
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute(
                    "SELECT device_id, user_id, device_name, activated_at, "
                    "status, online, last_seen FROM device_binding")
                rows = cur.fetchall()
            result = []
            for r in rows:
                result.append({
                    "device_id": r["device_id"],
                    "user_id": r["user_id"],
                    "device_name": r["device_name"],
                    "activated_at": r["activated_at"],
                    "status": r["status"],
                    "online": bool(r["online"]),
                    "last_seen": r["last_seen"],
                })
            return result
        except Exception as e:  # noqa: BLE001
            logger.warning("list_all failed: %s", e)
            return []

    def set_device_name(self, device_id, device_name):
        if not device_id:
            return
        try:
            self._ensure_table()
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO device_binding(device_id,device_name,status) "
                    "VALUES(?,?,'guest') "
                    "ON CONFLICT(device_id) DO UPDATE SET "
                    "device_name=excluded.device_name",
                    (device_id, device_name))
            self._refresh()
        except Exception as e:  # noqa: BLE001
            logger.warning("set_device_name failed for %s: %s", device_id, e)

    def issue_device_token(self, device_id, token=None):
        if not device_id:
            return None
        if not token:
            token = secrets.token_hex(24)
        try:
            self._ensure_columns()
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO device_binding(device_id,device_token) "
                    "VALUES(?,?) "
                    "ON CONFLICT(device_id) DO UPDATE SET "
                    "device_token=excluded.device_token",
                    (device_id, token))
            self._refresh()
            return token
        except Exception as e:  # noqa: BLE001
            logger.error("issue_device_token failed for %s: %s", device_id, e)
            return None

    def get_device_token(self, device_id):
        if not device_id:
            return None
        try:
            self._ensure_columns()
            with self._connect() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT device_token FROM device_binding WHERE device_id=?",
                    (device_id,))
                row = cur.fetchone()
            if row is None:
                return None
            return row[0]
        except Exception as e:  # noqa: BLE001
            logger.warning("get_device_token failed for %s: %s", device_id, e)
            return None

    def verify_device_token(self, device_id, token):
        if not device_id or not token:
            return False
        try:
            self._ensure_columns()
            with self._connect() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT device_token, status FROM device_binding "
                    "WHERE device_id=?",
                    (device_id,))
                row = cur.fetchone()
            if row is None:
                return False
            db_token, status = row
            if status != "active":
                return False
            if not db_token:
                return False
            return hmac.compare_digest(db_token, token)
        except Exception as e:  # noqa: BLE001
            logger.warning("verify_device_token failed for %s: %s", device_id, e)
            return False

    def clear_device_token(self, device_id):
        if not device_id:
            return
        try:
            self._ensure_columns()
            with self._connect() as conn:
                conn.execute(
                    "UPDATE device_binding SET device_token=NULL "
                    "WHERE device_id=?",
                    (device_id,))
            self._refresh()
        except Exception as e:  # noqa: BLE001
            logger.error("clear_device_token failed for %s: %s", device_id, e)

    def delete(self, device_id):
        if not device_id:
            return
        try:
            self._ensure_columns()
            with self._connect() as conn:
                # 精确大小写匹配，不做 normalize
                conn.execute(
                    "DELETE FROM device_binding WHERE device_id=?",
                    (device_id,))
            self._refresh()
        except Exception as e:  # noqa: BLE001
            logger.error("delete failed for %s: %s", device_id, e)


class DeviceAccess:
    """设备门禁：依据配置的策略决定某 device_id 是否放行。"""

    def __init__(self, config=None):
        iso = ((config or {}).get("server", {}) or {}).get(
            "device_isolation", {}) or {}
        self.enabled = bool(iso.get("enabled", False))
        self.policy = iso.get("policy", POLICY_ALLOW_ALL)
        self.store = DeviceBindingStore()

    def check(self, device_id):
        status = self.store.get_status(device_id)
        active = (status == "active")

        # 未启用 或 策略为全部放行
        if (not self.enabled) or (self.policy == POLICY_ALLOW_ALL):
            return (True, "", "active" if active else "guest")
        # 访客允许策略
        if self.policy == POLICY_GUEST_ALLOWED:
            return (True, "", "active" if active else "guest")
        # 要求激活策略
        if self.policy == POLICY_REQUIRE_ACTIVATION:
            if active:
                return (True, "", "active")
            # 表缺失 / 无数据 -> 降级放行
            if not self.store.is_available():
                return (True, "", "guest")
            return (False, "device not activated", "inactive")
        # 未知策略 -> 安全默认（放行）
        return (True, "", "active" if active else "guest")
