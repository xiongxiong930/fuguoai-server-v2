"""设备「实时在线态」持久化旁路模块（clean-room 原生实现）。

仅负责把 online / last_seen 落库到 ``device_binding`` 表，供外部 manage 服务读取。
纯标准库实现，不依赖任何 fuguoai_core / fuguoai_config 内部模块。

行为要点：
- 异常不吞：三个同步函数均只保证 finally 关闭连接，sqlite 异常原样向上传播。
- init_schema 只 ALTER 补列，绝不 CREATE TABLE。
"""

import asyncio
import os
import sqlite3
import time
from typing import Optional

# 本模块文件所在目录上溯 3 级 = 仓库根，再拼 data/device_agent.db
DEVICE_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
    "device_agent.db",
)

# SQLite 写锁等待上限（秒），避免与 manage 服务并发互相拖死
_SQLITE_TIMEOUT = 5.0


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(DEVICE_DB_PATH, timeout=_SQLITE_TIMEOUT)


def init_schema() -> None:
    """给已存在的 device_binding 表补 online / last_seen 两列（幂等，不建表）。"""
    conn = _connect()
    try:
        cur = conn.execute("PRAGMA table_info(device_binding)")
        cols = {row[1] for row in cur.fetchall()}
        if "online" not in cols:
            conn.execute("ALTER TABLE device_binding ADD COLUMN online INTEGER DEFAULT 0")
        if "last_seen" not in cols:
            conn.execute("ALTER TABLE device_binding ADD COLUMN last_seen INTEGER DEFAULT 0")
        conn.commit()
    finally:
        conn.close()


def set_online(device_id: str, online: int, last_seen: Optional[int] = None) -> None:
    """置某设备的在线态；device_id 为空直接返回。"""
    if not device_id:
        return
    if last_seen is None:
        last_seen = int(time.time())
    conn = _connect()
    try:
        conn.execute(
            "UPDATE device_binding SET online=?, last_seen=? WHERE device_id=?",
            (online, last_seen, device_id),
        )
        conn.commit()
    finally:
        conn.close()


def offline_stale(cutoff: int) -> None:
    """把 last_seen < cutoff 的陈旧在线行置为离线。"""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE device_binding SET online=0 WHERE online=1 AND last_seen<?",
            (cutoff,),
        )
        conn.commit()
    finally:
        conn.close()


async def init_schema_async() -> None:
    await asyncio.to_thread(init_schema)


async def set_online_async(device_id: str, online: int, last_seen: Optional[int] = None) -> None:
    await asyncio.to_thread(set_online, device_id, online, last_seen)


async def offline_stale_async(cutoff: int) -> None:
    await asyncio.to_thread(offline_stale, cutoff)
