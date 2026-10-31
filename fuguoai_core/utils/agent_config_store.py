"""M12 · 设备智能体配置：SQLite 持久化（每台设备一份角色配置）（自研重构版）。

存储结构：`data/device_agent.db` 的 `device_agent_config` 表，与 M11 的
`device_binding`、M9 的 `device_usage` 共用同一 SQLite 文件，零外部依赖。

约定（写进《长期维护更新计划》§八）
- 一功能一 SQLite 单例；通过 `get_agent_config_store()` 获取全局单例。
- 非法字段由调用方（routes）校验，store 只负责落库与合并。
- 任意异常均降级返回，绝不向上抛，避免影响连接/请求主流程。

相对 fork 起点的改进（契约不变）：
- 每线程连接缓存 + WAL（高发读写更稳，避免每次调用 open/close）。
- 连接层抽成 `_connect()`；所有读写经 `_safe()` 包裹，异常降级。
- 公开 API / KNOWN_COLUMNS / SQL schema 完全兼容调用方与线上库文件。
"""
import json
import logging
import os
import sqlite3
import threading
import time

from fuguoai_config.config_loader import get_project_dir

TAG = __name__
logger = logging.getLogger(TAG)

# 每线程一把连接（配合 WAL，多线程读写安全）
_thread_local = threading.local()

# 表中持久化的已知列（其余扩展字段走 raw_json）
KNOWN_COLUMNS = (
    "device_id", "user_id", "wakeup_words", "prompt",
    "tts_provider", "voice", "speed", "volume", "tone", "dialect",
    "active_preset",
)

_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS device_agent_config ("
    "device_id TEXT PRIMARY KEY,"
    "user_id TEXT,"
    "wakeup_words TEXT,"
    "prompt TEXT,"
    "tts_provider TEXT,"
    "voice TEXT,"
    "speed REAL,"
    "volume REAL,"
    "tone TEXT,"
    "dialect TEXT,"
    "updated_at INTEGER,"
    "raw_json TEXT)"
)

_COLS = (
    "device_id", "user_id", "wakeup_words", "prompt", "tts_provider",
    "voice", "speed", "volume", "tone", "dialect", "active_preset",
    "updated_at", "raw_json",
)


class AgentConfigStore:
    """设备智能体配置存储。线程安全（每线程独立 SQLite 连接 + WAL）。"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or (get_project_dir() + "data/device_agent.db")
        d = os.path.dirname(self.db_path)
        if d:
            os.makedirs(d, exist_ok=True)
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
            self._ensure_table(conn)
        return conn

    def _ensure_table(self, conn: sqlite3.Connection) -> None:
        try:
            conn.execute(_TABLE_DDL)
            conn.commit()
            # M15：迁移新增 active_preset 反查列（已存在则忽略）
            try:
                conn.execute(
                    "ALTER TABLE device_agent_config ADD COLUMN active_preset TEXT"
                )
                conn.commit()
            except sqlite3.Error:
                pass
        except sqlite3.Error as e:
            logger.warning(f"device_agent_config 表初始化失败: {e}")

    def _safe(self, fn):
        try:
            return fn(self._connect())
        except sqlite3.Error as e:
            logger.warning(f"device_agent_config 操作失败: {e}")
            return None

    # ---------------- 读 ----------------
    def get_config(self, device_id: str):
        """返回该设备配置 dict；无记录返回 None。"""
        if not device_id:
            return None

        def _do(conn):
            cur = conn.execute(
                "SELECT device_id, user_id, wakeup_words, prompt, tts_provider, "
                "voice, speed, volume, tone, dialect, active_preset, updated_at, raw_json "
                "FROM device_agent_config WHERE device_id=?",
                (device_id,),
            )
            return cur.fetchone()

        r = self._safe(_do)
        if not r:
            return None
        return self._row_to_dict(r)

    # ---------------- 写（PATCH 语义）----------------
    def put_config(self, device_id: str, fields: dict, user_id: str = None):
        """PATCH 语义：仅更新 fields 中的字段，未给字段保持原值。返回合并后的完整行。"""
        if not device_id:
            raise ValueError("device_id 不能为空")
        existing = self.get_config(device_id) or {}
        merged = dict(existing)
        for k in KNOWN_COLUMNS:
            if k in ("device_id", "updated_at", "raw_json"):
                continue
            if k in fields:
                merged[k] = fields[k]
        # 收集未映射的扩展字段
        extra = {k: v for k, v in fields.items() if k not in KNOWN_COLUMNS}
        merged["raw_json"] = json.dumps(extra, ensure_ascii=False) if extra else None
        if user_id is not None:
            merged["user_id"] = user_id
        merged["updated_at"] = int(time.time())

        try:
            conn = self._connect()
            conn.execute(
                "INSERT INTO device_agent_config("
                "device_id, user_id, wakeup_words, prompt, tts_provider, voice, "
                "speed, volume, tone, dialect, active_preset, updated_at, raw_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(device_id) DO UPDATE SET "
                "user_id=excluded.user_id, wakeup_words=excluded.wakeup_words, "
                "prompt=excluded.prompt, tts_provider=excluded.tts_provider, "
                "voice=excluded.voice, speed=excluded.speed, volume=excluded.volume, "
                "tone=excluded.tone, dialect=excluded.dialect, "
                "active_preset=excluded.active_preset, "
                "updated_at=excluded.updated_at, raw_json=excluded.raw_json",
                (
                    device_id, merged.get("user_id"), merged.get("wakeup_words"),
                    merged.get("prompt"), merged.get("tts_provider"), merged.get("voice"),
                    merged.get("speed"), merged.get("volume"), merged.get("tone"),
                    merged.get("dialect"), merged.get("active_preset"),
                    merged.get("updated_at"), merged.get("raw_json"),
                ),
            )
            conn.commit()
        except sqlite3.Error as e:
            logger.error(f"写入设备配置失败: {e}")
            raise
        return self.get_config(device_id)

    def delete_config(self, device_id: str) -> None:
        """删除设备配置，回落全局。"""
        if not device_id:
            return

        def _do(conn):
            conn.execute(
                "DELETE FROM device_agent_config WHERE device_id=?", (device_id,)
            )
            conn.commit()

        self._safe(_do)

    # ---------------- 列表 ----------------
    def list_for_user(self, user_id: str) -> list:
        """返回该用户名下全部设备配置列表（按 updated_at 倒序）。"""
        if not user_id:
            return []
        return self._query("WHERE user_id=? ORDER BY updated_at DESC", (user_id,))

    def list_all_configured(self) -> list:
        """返回全部已配置设备（管理/调试用）。"""
        return self._query("ORDER BY updated_at DESC", ())

    def _query(self, where: str, params: tuple) -> list:
        def _do(conn):
            cur = conn.execute(
                "SELECT device_id, user_id, wakeup_words, prompt, tts_provider, "
                "voice, speed, volume, tone, dialect, active_preset, updated_at, raw_json "
                "FROM device_agent_config " + where,
                params,
            )
            return [self._row_to_dict(r) for r in cur.fetchall()]

        res = self._safe(_do)
        return res or []

    @staticmethod
    def _row_to_dict(r) -> dict:
        return {
            "device_id": r[0],
            "user_id": r[1],
            "wakeup_words": r[2],
            "prompt": r[3],
            "tts_provider": r[4],
            "voice": r[5],
            "speed": r[6],
            "volume": r[7],
            "tone": r[8],
            "dialect": r[9],
            "active_preset": r[10],
            "updated_at": r[11],
            "raw_json": json.loads(r[12]) if r[12] else None,
        }


# 全局单例，供 routes 与 connection overlay 共享
_AGENT_CONFIG_STORE: "AgentConfigStore | None" = None


def get_agent_config_store() -> AgentConfigStore:
    global _AGENT_CONFIG_STORE
    if _AGENT_CONFIG_STORE is None:
        _AGENT_CONFIG_STORE = AgentConfigStore()
    return _AGENT_CONFIG_STORE
