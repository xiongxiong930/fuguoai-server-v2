"""M9 · 用量计量：SQLite 持久化每会话用量记录（自研重构版）。

存储结构：`data/device_agent.db` 的 `device_usage` 表，每会话一行。
start_ts / end_ts 供时长计算；turn_count / response_chars 供调用量与字符量统计。
get_daily_summary() 聚合当日各设备汇总，供 /admin/summary 消费。

相对 fork 起点的改进（行为不变，仅实现进化）：
- 连接管理：每个线程缓存一把连接（threading.local），避免每次调用都 open/close；
  连接建立即开 WAL + busy_timeout，提升并发读写稳定性。
- 错误降级语义不变：任何异常都降级返回，绝不向上抛断连接主流程。
- 公开 API（类名 / 方法签名 / SQL schema）完全兼容既有调用方与线上库文件。
"""

import logging
import os
import sqlite3
import threading
import time

from fuguoai_config.config_loader import get_project_dir

TAG = __name__
logger = logging.getLogger(TAG)

# 每线程一把连接，避免高频 open/close，并确保多线程安全（配合 WAL）
_thread_local = threading.local()


# ── 当前对话的设备/会话上下文（contextvar）──
# openai provider 在 LLM 调用拿到 usage 时，需要知道这笔 token 归属哪个设备/会话。
# LLM provider 可能是连接间共享的单例（见 providers/memory/mem_local_short 注释），
# 不能把 device/session 挂到 provider 实例上；改用 contextvar：连接层在每轮 handleTextMessage
# 前 set，provider 侧 get 后直写用量库，天然按异步任务隔离，规避并发覆盖。
import contextvars
_current_device_session = contextvars.ContextVar("fuguoai_device_session", default=None)


def set_current_device_session(device_id: str, session_id: str):
    """在当前异步任务上下文标记 device/session，供 provider 直写用量库。"""
    return _current_device_session.set((device_id, session_id))


def reset_current_device_session(token) -> None:
    _current_device_session.reset(token)


def get_current_device_session():
    return _current_device_session.get()


def _today_start() -> int:
    """返回今日 00:00:00（本地时区）的时间戳（秒）。"""
    t = time.localtime()
    return int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, 0)))


class DeviceUsageStore:
    """每会话用量存储。线程安全（每线程独立 SQLite 连接 + WAL）。"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or (get_project_dir() + "data/device_agent.db")
        self._ensure_dir()

    # ---------------- 连接管理 ----------------
    def _ensure_dir(self) -> None:
        d = os.path.dirname(self.db_path)
        if d:
            os.makedirs(d, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        """返回当前线程缓存的连接；首次访问时建连并开启 WAL。"""
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

    def _ensure_table(self, conn: sqlite3.Connection = None) -> None:
        target = conn or self._connect()
        try:
            target.execute(
                "CREATE TABLE IF NOT EXISTS device_usage ("
                "device_id TEXT NOT NULL,"
                "session_id TEXT NOT NULL,"
                "start_ts INTEGER NOT NULL,"
                "end_ts INTEGER,"
                "duration_seconds REAL,"
                "turn_count INTEGER DEFAULT 0,"
                "response_chars INTEGER DEFAULT 0,"
                "PRIMARY KEY (device_id, session_id)"
                ")"
            )
            # 用量查询大量按 start_ts 区间过滤（get_daily_summary / get_weekly_summary /
            # get_device_summary），无索引会导致全表扫描；对讲会话产生大量 device_usage
            # 记录后，设备列表查询会退化为 N 次全表扫描 → 慢查询 + SQLite 锁竞争。
            # 加索引是幂等的（IF NOT EXISTS），对存量库安全。
            target.execute(
                "CREATE INDEX IF NOT EXISTS idx_device_usage_start_ts "
                "ON device_usage(start_ts)"
            )
            # 新增 token 计量列（LLM 输入/输出 token）；存量库已建表时无此列，
            # 用 ALTER ADD COLUMN（包裹重复列异常忽略）幂等补列，对线上库安全。
            for _col in ("prompt_tokens", "completion_tokens"):
                try:
                    target.execute(
                        f"ALTER TABLE device_usage ADD COLUMN {_col} INTEGER DEFAULT 0"
                    )
                except sqlite3.OperationalError:
                    pass  # 列已存在
            target.commit()
        except sqlite3.Error as e:
            logger.warning(f"device_usage 表初始化失败: {e}")

    def _safe(self, fn):
        """包裹一次 DB 操作：异常降级，绝不向上抛。"""
        try:
            return fn(self._connect())
        except sqlite3.Error as e:
            logger.warning(f"device_usage 操作失败: {e}")
            return None

    # ---------------- 写入 ----------------
    def record_session_start(self, device_id: str, session_id: str) -> None:
        if not device_id or not session_id:
            return

        def _do(conn):
            conn.execute(
                "INSERT OR IGNORE INTO device_usage(device_id, session_id, start_ts) "
                "VALUES(?, ?, ?)",
                (device_id, session_id, int(time.time())),
            )
            conn.commit()

        self._safe(_do)

    def record_turn(self, device_id: str, session_id: str, response_chars: int = 0) -> None:
        if not device_id or not session_id:
            return

        def _do(conn):
            conn.execute(
                "UPDATE device_usage SET turn_count = turn_count + 1, "
                "response_chars = response_chars + ? "
                "WHERE device_id = ? AND session_id = ?",
                (response_chars, device_id, session_id),
            )
            conn.commit()

        self._safe(_do)

    def record_llm_tokens(self, device_id: str, session_id: str, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        if not device_id or not session_id:
            return
        pt = int(prompt_tokens or 0)
        ct = int(completion_tokens or 0)
        if pt <= 0 and ct <= 0:
            return

        def _do(conn):
            conn.execute(
                "UPDATE device_usage SET prompt_tokens = prompt_tokens + ?, "
                "completion_tokens = completion_tokens + ? "
                "WHERE device_id = ? AND session_id = ?",
                (pt, ct, device_id, session_id),
            )
            conn.commit()

        self._safe(_do)

    def record_session_end(self, device_id: str, session_id: str) -> None:
        if not device_id or not session_id:
            return

        def _do(conn):
            now = int(time.time())
            conn.execute(
                "UPDATE device_usage SET end_ts = ?, duration_seconds = "
                "CAST(? AS REAL) - start_ts WHERE device_id = ? AND session_id = ?",
                (now, now, device_id, session_id),
            )
            conn.commit()

        self._safe(_do)

    # ---------------- 查询 ----------------
    def get_daily_summary(self, today_start: int = None) -> list:
        """返回当日设备级汇总列表，每条含 device_id, sessions, turns, chars, duration。"""
        if today_start is None:
            today_start = _today_start()

        rows = self._safe(
            lambda conn: conn.execute(
                "SELECT device_id, COUNT(*) as sessions, "
                "COALESCE(SUM(turn_count),0) as turns, "
                "COALESCE(SUM(response_chars),0) as chars, "
                "COALESCE(SUM(prompt_tokens),0) as prompt_tokens, "
                "COALESCE(SUM(completion_tokens),0) as completion_tokens, "
                "COALESCE(SUM(duration_seconds),0.0) as duration "
                "FROM device_usage WHERE start_ts >= ? "
                "GROUP BY device_id ORDER BY turns DESC",
                (today_start,),
            ).fetchall()
        )
        if rows is None:
            return []
        return [
            {
                "device_id": r[0],
                "sessions": r[1],
                "turns": r[2],
                "response_chars": r[3],
                "prompt_tokens": r[4],
                "completion_tokens": r[5],
                "duration_seconds": round(r[6], 1),
            }
            for r in rows
        ]

    def get_weekly_summary(self, days: int = 7) -> list:
        """返回近 days 天设备级用量汇总列表（批量聚合）。

        与 get_daily_summary 等价，但窗口为近 days 天而非仅当日。
        供设备列表一次性拉取全部设备的近 7 天用量，消除 handle_list 中对
        get_device_summary 的 N+1 循环调用（每个设备一次全表扫描）。
        """
        since = int(time.time()) - days * 86400

        rows = self._safe(
            lambda conn: conn.execute(
                "SELECT device_id, COUNT(*) as sessions, "
                "COALESCE(SUM(turn_count),0) as turns, "
                "COALESCE(SUM(response_chars),0) as chars, "
                "COALESCE(SUM(prompt_tokens),0) as prompt_tokens, "
                "COALESCE(SUM(completion_tokens),0) as completion_tokens, "
                "COALESCE(SUM(duration_seconds),0.0) as duration, "
                "MAX(start_ts) as last_active_ts "
                "FROM device_usage WHERE start_ts >= ? "
                "GROUP BY device_id ORDER BY turns DESC",
                (since,),
            ).fetchall()
        )
        if rows is None:
            return []
        return [
            {
                "device_id": r[0],
                "sessions": r[1],
                "turns": r[2],
                "response_chars": r[3],
                "prompt_tokens": r[4],
                "completion_tokens": r[5],
                "duration_seconds": round(r[6], 1),
                "last_active_ts": int(r[7]) if r[7] else None,
            }
            for r in rows
        ]

    def get_last_active(self, device_id: str):
        """该设备最近一次活跃时间戳（秒）；无记录返回 None。取 end_ts/start_ts 最大值。"""
        if not device_id:
            return None

        def _do(conn):
            cur = conn.execute(
                "SELECT COALESCE(MAX(COALESCE(end_ts,0)),0), COALESCE(MAX(start_ts),0) "
                "FROM device_usage WHERE device_id = ?",
                (device_id,),
            )
            return cur.fetchone()

        r = self._safe(_do)
        if not r:
            return None
        return max(r[0], r[1]) or None

    def get_device_summary(self, device_id: str, days: int = 1):
        """返回该设备近 days 天的用量汇总 + 最近活跃时间；无记录返回 None。"""
        since = int(time.time()) - days * 86400

        def _do(conn):
            cur = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(turn_count),0), "
                "COALESCE(SUM(response_chars),0), COALESCE(SUM(prompt_tokens),0), "
                "COALESCE(SUM(completion_tokens),0), COALESCE(SUM(duration_seconds),0.0), "
                "MAX(start_ts) FROM device_usage "
                "WHERE device_id=? AND start_ts>=?",
                (device_id, since),
            )
            return cur.fetchone()

        r = self._safe(_do)
        if not r or r[0] == 0:
            return None
        return {
            "sessions": int(r[0]),
            "turns": int(r[1]),
            "response_chars": int(r[2]),
            "prompt_tokens": int(r[3]),
            "completion_tokens": int(r[4]),
            "duration_seconds": round(r[5], 1),
            "last_active_ts": int(r[6]) if r[6] else None,
        }

    def get_all_device_ids(self) -> list:
        """返回所有出现过的 device_id（去重）。表缺失返回 []。"""

        def _do(conn):
            return [row[0] for row in conn.execute(
                "SELECT DISTINCT device_id FROM device_usage"
            ).fetchall()]

        res = self._safe(_do)
        return res or []


# 全局单例，供 connection 与 http_server 共享
_USAGE_STORE: "DeviceUsageStore | None" = None


def get_usage_store() -> DeviceUsageStore:
    global _USAGE_STORE
    if _USAGE_STORE is None:
        _USAGE_STORE = DeviceUsageStore()
    return _USAGE_STORE
