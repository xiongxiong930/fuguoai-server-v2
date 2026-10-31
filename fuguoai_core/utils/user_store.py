"""微信小程序用户存储（SQLite 持久化）。

- 存储位置：默认落在 ``get_project_dir() + "data/device_agent.db"`` 的 ``users`` 表内，
  维护微信 ``openid`` 与内部 ``user_id`` 的映射；也可在构造时显式传入 ``db_path``。
- 单例获取：通过模块级函数 :func:`get_user_store` 取得全局唯一实例，
  首次调用时惰性创建，后续调用复用同一对象。
- 异常降级约定：查询类操作（``get_by_openid`` / ``get_by_user_id``）遇到任何异常时
  记录 warning 并返回 ``None``，不向调用方抛出；写入类操作（``upsert``）在记录 error
  后必须重新抛出异常，以便上层感知持久化失败。
"""

import hashlib
import logging
import os
import sqlite3
import time

from fuguoai_config.config_loader import get_project_dir

TAG = __name__


class UserStore(object):
    """基于 SQLite 的用户表读写封装。"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or (get_project_dir() + "data/device_agent.db")
        self.logger = logging.getLogger(TAG)
        self._ensure_table()

    def _ensure_table(self):
        try:
            d = os.path.dirname(self.db_path)
            if d:
                os.makedirs(d, exist_ok=True)
            conn = sqlite3.connect(self.db_path, timeout=2)
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS users ("
                    "openid TEXT PRIMARY KEY, "
                    "user_id TEXT UNIQUE, "
                    "nickname TEXT, "
                    "avatar TEXT, "
                    "phone TEXT, "
                    "created_at INTEGER, "
                    "updated_at INTEGER"
                    ")"
                )
                conn.commit()
                # 兼容缺少 phone 列的旧库：列已存在时必然失败，静默忽略
                try:
                    conn.execute("ALTER TABLE users ADD COLUMN phone TEXT")
                    conn.commit()
                except Exception:
                    pass
            finally:
                conn.close()
        except Exception as e:
            self.logger.warning(f"users 表初始化失败: {e}")

    @staticmethod
    def _gen_user_id(openid: str) -> str:
        return "U" + hashlib.sha256(openid.encode("utf-8")).hexdigest()[:12]

    def upsert(self, openid: str, nickname: str = None, avatar: str = None, phone: str = None) -> dict:
        if not openid:
            raise ValueError("openid 不能为空")
        now = int(time.time())
        existing = self.get_by_openid(openid)
        try:
            conn = sqlite3.connect(self.db_path, timeout=2, check_same_thread=False)
            try:
                if existing:
                    sets = []
                    params = []
                    if nickname is not None:
                        sets.append("nickname=?")
                        params.append(nickname)
                    if avatar is not None:
                        sets.append("avatar=?")
                        params.append(avatar)
                    if phone is not None:
                        sets.append("phone=?")
                        params.append(phone)
                    sets.append("updated_at=?")
                    params.append(now)
                    params.append(openid)
                    conn.execute(
                        "UPDATE users SET " + ",".join(sets) + " WHERE openid=?",
                        tuple(params),
                    )
                else:
                    conn.execute(
                        "INSERT INTO users"
                        "(openid, user_id, nickname, avatar, phone, created_at, updated_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (
                            openid,
                            self._gen_user_id(openid),
                            nickname,
                            avatar,
                            phone,
                            now,
                            now,
                        ),
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            self.logger.error(f"写入用户失败: {e}")
            raise
        return self.get_by_openid(openid) or {
            "openid": openid,
            "user_id": self._gen_user_id(openid),
            "nickname": nickname,
            "avatar": avatar,
            "phone": phone,
        }

    def get_by_openid(self, openid: str):
        return self._get("openid=?", (openid,))

    def get_by_user_id(self, user_id: str):
        return self._get("user_id=?", (user_id,))

    def _get(self, where: str, params: tuple):
        try:
            conn = sqlite3.connect(self.db_path, timeout=2, check_same_thread=False)
            try:
                cur = conn.execute(
                    "SELECT openid, user_id, nickname, avatar, phone, created_at, updated_at "
                    "FROM users WHERE " + where,
                    params,
                )
                r = cur.fetchone()
                if not r:
                    return None
                return {
                    "openid": r[0],
                    "user_id": r[1],
                    "nickname": r[2],
                    "avatar": r[3],
                    "phone": r[4],
                    "created_at": r[5],
                    "updated_at": r[6],
                }
            finally:
                conn.close()
        except Exception as e:
            self.logger.warning(f"查询用户失败: {e}")
            return None


_USER_STORE: UserStore = None


def get_user_store() -> UserStore:
    global _USER_STORE
    if _USER_STORE is None:
        _USER_STORE = UserStore()
    return _USER_STORE
