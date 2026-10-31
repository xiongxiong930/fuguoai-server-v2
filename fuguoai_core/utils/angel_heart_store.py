"""天使心 App 内容层存储（记忆痕迹 / 首页心核 / 手帐本 / 用户档案）。

对应《天使心后端接口文档 v0.1》§7/§8/§9/§10——这几节此前**只有文档、无代码**
（代码里 grep「手帐」零命中），本模块把它们落成可跑的后端。

设计要点
--------
- 四块数据各一份 JSON，落在 data/angel_heart/ 下；沿用 character_store 同范式：
  threading 锁 + 写 tmp 文件 + os.replace 原子替换（避免写一半被读）。
- **全部按 uid 分桶**：手帐 / 记忆 / 心核 / 偏好都是用户私有数据，越权一律返回
  404 或空列表，不泄露「资源是否存在」。
- 全程 fail-soft：文件不存在或 JSON 损坏按空库处理，绝不让服务起不来。
- 与角色线的集成：首页心核的 primary_character **直接读 CharacterSessionStore**
  （懒导入 + try），与 NFC 碰卡 / 小程序激活的角色保持单一数据源，不另存一份
  避免漂移。角色目录缺失时该项为 None，不影响其余字段。

持久化路径受 FUGUOAI_DATA_DIR 控制（默认 /opt/fuguoai-server/data），
与 character_store / NetworkedReminderStore 一致。
"""

import json
import os
import threading
import time
import uuid

from fuguoai_config.logger import setup_logging

TAG = __name__
logger = setup_logging()

_DATA_DIR = os.environ.get("FUGUOAI_DATA_DIR", "/opt/fuguoai-server/data")
_BASE = os.path.join(_DATA_DIR, "angel_heart")

_JOURNALS_PATH = os.path.join(_BASE, "journals.json")
_MEMORIES_PATH = os.path.join(_BASE, "memories.json")
_HOME_PATH = os.path.join(_BASE, "home.json")
_USERS_PATH = os.path.join(_BASE, "users.json")
_PERSONA_PATH = os.path.join(_BASE, "persona_offsets.json")

# 手帐分类（文档 §9：全部 / 日常 / 穿搭 / 特别的日子 / 草稿）
# 中英文都收，前端传哪种都能认；"草稿" 是独立 bool 标志，此处映射为特殊 category 便于筛选。
_CATEGORY_ALIASES = {
    "daily": "daily", "日常": "daily",
    "outfit": "outfit", "穿搭": "outfit",
    "special": "special", "特别的日子": "special",
    "draft": "draft", "草稿": "draft",
}
# 新建/编辑时允许的分类（不含 draft——草稿由 draft 布尔位表达）
_CATEGORIES = ("daily", "outfit", "special")
_VISIBILITIES = ("public", "private")

# 心的资料覆盖（前端 §10 契约）：prefs 里的键名 + 可写白名单。
# characters.yaml 由 admin 维护、拒客户端写，所以用户对某颗心的改名/改人格/改音色
# 只能作为「按 uid 的覆盖层」存在——落在 preferences 里，不动角色目录本身。
_OVERRIDES_KEY = "character_overrides"
# 可覆盖字段白名单（按 cid 合并）。2026-10-16 增 "voice_type"：活字匣前端
# PG-TUNE 调校面板存的键名是 voice_type（腾讯 VoiceType 数字 ID），与旧键
# voice 在本部署同值（yaml voice 即数字 ID），两个键都收，前端用哪个都进。
_OVERRIDE_FIELDS = ("name", "personality", "voice", "voice_type", "led_config", "awaken_sound")

# 性格偏移层（P2）：结构化维度，每维有界；基线 system_prompt 永不改写
# （防漂移 / injection / 跨设备复用破坏）。偏移由后端对话抽取增量更新
# （未来 P1/P2 抽取管线调 apply_drift），客户端**只能查看 + 一键重置**，
# 不能自由写 system_prompt / 任意维度。
_PERSONA_DIMS = ("intimacy", "liveliness", "empathy", "humor", "length")
_PERSONA_BOUNDS = {
    "intimacy": (-1.0, 1.0),   # 亲密度
    "liveliness": (-1.0, 1.0),  # 活泼度
    "empathy": (-1.0, 1.0),     # 共情强度
    "humor": (-1.0, 1.0),       # 幽默度
    "length": (-1.0, 1.0),      # 表达长度（偏长 + / 偏短 -）
}
_PERSONA_LABELS = {
    "intimacy": "亲密度",
    "liveliness": "活泼度",
    "empathy": "共情强度",
    "humor": "幽默度",
    "length": "表达长度",
}


def _now():
    return time.time()


def _new_id():
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# 基类：JSON 文件 + 锁 + 原子写
# ---------------------------------------------------------------------------
class _JsonStore:
    def __init__(self, path):
        self._path = path
        self._lock = threading.Lock()

    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        except Exception as e:
            logger.bind(tag=TAG).error(f"读取 {self._path} 失败，按空库处理: {e}")
            return {}

    def _save(self, db):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)


# ---------------------------------------------------------------------------
# 手帐本（§9）
# ---------------------------------------------------------------------------
class JournalStore(_JsonStore):
    """uid → [journal]。默认私有（visibility=private）。"""

    def list(self, uid, category=None, draft=None):
        """category="draft" 表示只要草稿；draft 为 True/False 时按布尔位过滤。"""
        items = self._load().get(uid, [])
        out = []
        for j in items:
            if category == "draft":
                if not j.get("draft"):
                    continue
            elif category and j.get("category") != category:
                continue
            if draft is not None and bool(j.get("draft")) != draft:
                continue
            out.append(j)
        out.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
        return out

    def get(self, uid, journal_id):
        for j in self._load().get(uid, []):
            if j.get("id") == journal_id:
                return j
        return None

    def create(self, uid, data):
        now = _now()
        j = {
            "id": _new_id(),
            "uid": uid,
            "category": _CATEGORY_ALIASES.get(data.get("category"), "daily"),
            "draft": bool(data.get("draft", False)),
            "visibility": "public" if data.get("visibility") == "public" else "private",
            "title": (data.get("title") or "").strip(),
            # 富媒体：贴纸 / 照片 / 文字 / 涂鸦 / 背景，整体作为一个 dict 存
            "content": data.get("content") if isinstance(data.get("content"), dict) else {},
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            db = self._load()
            db.setdefault(uid, []).append(j)
            self._save(db)
        return j

    def update(self, uid, journal_id, patch):
        with self._lock:
            db = self._load()
            for j in db.get(uid, []):
                if j.get("id") == journal_id:
                    if "category" in patch:
                        c = _CATEGORY_ALIASES.get(patch["category"])
                        if c is None or c == "draft":
                            raise ValueError(f"category 必须是 {_CATEGORIES}")
                        j["category"] = c
                    if "draft" in patch:
                        j["draft"] = bool(patch["draft"])
                    if "title" in patch:
                        j["title"] = (patch["title"] or "").strip()
                    if "content" in patch:
                        j["content"] = patch["content"] if isinstance(patch["content"], dict) else {}
                    j["updated_at"] = _now()
                    self._save(db)
                    return j
        return None

    def delete(self, uid, journal_id):
        with self._lock:
            db = self._load()
            items = db.get(uid, [])
            for i, j in enumerate(items):
                if j.get("id") == journal_id:
                    del items[i]
                    self._save(db)
                    return True
        return False

    def set_visibility(self, uid, journal_id, visibility):
        if visibility not in _VISIBILITIES:
            raise ValueError(f"visibility 必须是 {_VISIBILITIES}")
        with self._lock:
            db = self._load()
            for j in db.get(uid, []):
                if j.get("id") == journal_id:
                    j["visibility"] = visibility
                    j["updated_at"] = _now()
                    self._save(db)
                    return j
        return None


# ---------------------------------------------------------------------------
# 记忆痕迹（§7）
# ---------------------------------------------------------------------------
class MemoryStore(_JsonStore):
    """uid → character_id → [fact]。角色沉淀的长期记忆事实。"""

    def list(self, uid, character_id):
        return self._load().get(uid, {}).get(character_id, [])

    def list_all(self, uid, limit=20):
        """首页记忆流：跨角色聚合，按时间倒序。"""
        out = []
        for cid, facts in self._load().get(uid, {}).items():
            for f in facts:
                item = dict(f)
                item.setdefault("character_id", cid)
                out.append(item)
        out.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        return out[:limit]

    def add(self, uid, character_id, fact):
        item = {
            "id": _new_id(),
            "character_id": character_id,
            "fact": fact,
            "created_at": _now(),
        }
        with self._lock:
            db = self._load()
            db.setdefault(uid, {}).setdefault(character_id, []).append(item)
            self._save(db)
        return item

    def delete(self, uid, memory_id):
        """按 uid 跨角色找该 memory_id 并删除（客户端只知道自己的 id）。
        返回 True 表示删到了；False 表示本 uid 下无此 id——**不泄露是否存在**，
        对外统一 404。"""
        with self._lock:
            db = self._load()
            user_mem = db.get(uid)
            if not user_mem:
                return False
            for cid, facts in user_mem.items():
                for i, f in enumerate(facts):
                    if f.get("id") == memory_id:
                        del facts[i]
                        self._save(db)
                        return True
            return False

    def delete_all(self, uid, character_id):
        """清空某 uid+character 下的全部记忆。返回删除条数（0 表示本来就没有）。"""
        with self._lock:
            db = self._load()
            user_mem = db.get(uid)
            if not user_mem or character_id not in user_mem:
                return 0
            n = len(user_mem[character_id])
            user_mem[character_id] = []
            self._save(db)
            return n


# ---------------------------------------------------------------------------
# 性格偏移层（P2）：有界维度，基线锁定
# ---------------------------------------------------------------------------
class PersonaOffsetStore(_JsonStore):
    """uid → character_id → {offsets:{dim:float}, updated_at}。

    客户端只读 + 一键重置；偏移值由后端抽取管线经 apply_drift 增量写入（clamp 到
    _bounds）。基线 system_prompt 永不经此改写——偏移只在注入时叠加，便于一键回初始。
    """

    def get(self, uid, character_id):
        entry = self._load().get(uid, {}).get(character_id)
        offsets = dict((entry or {}).get("offsets") or {}) if entry else {}
        return {
            "character_id": character_id,
            "offsets": {d: float(offsets.get(d, 0.0)) for d in _PERSONA_DIMS},
            "dims": _PERSONA_LABELS,
            "bounds": {d: list(_PERSONA_BOUNDS[d]) for d in _PERSONA_DIMS},
            "baseline_locked": True,
            "updated_at": (entry or {}).get("updated_at", 0),
        }

    def reset(self, uid, character_id):
        """一键恢复初始：所有维度归零、基线不动。返回重置后状态。"""
        with self._lock:
            db = self._load()
            db.setdefault(uid, {})[character_id] = {
                "offsets": {d: 0.0 for d in _PERSONA_DIMS},
                "updated_at": _now(),
            }
            self._save(db)
        return self.get(uid, character_id)

    def apply_drift(self, uid, character_id, deltas):
        """后端抽取管线调用（**非 REST 暴露**）：对每维做有界增量更新。
        越界按 _PERSONA_BOUNDS clamp，保留 4 位精度。"""
        if not isinstance(deltas, dict):
            return self.get(uid, character_id)
        with self._lock:
            db = self._load()
            user = db.setdefault(uid, {})
            entry = user.get(character_id) or {"offsets": {d: 0.0 for d in _PERSONA_DIMS}}
            offs = dict(entry.get("offsets") or {})
            for d in _PERSONA_DIMS:
                if d not in deltas:
                    continue
                cur = float(offs.get(d, 0.0))
                lo, hi = _PERSONA_BOUNDS[d]
                try:
                    delta = float(deltas[d] or 0.0)
                except (TypeError, ValueError):
                    continue
                nxt = max(lo, min(hi, cur + delta))
                offs[d] = round(nxt, 4)
            user[character_id] = {"offsets": offs, "updated_at": _now()}
            self._save(db)
        return self.get(uid, character_id)


# ---------------------------------------------------------------------------
# 首页 / 心核（§8）
# ---------------------------------------------------------------------------
class HomeStore(_JsonStore):
    """uid → {core, diy}。心核状态 + DIY 外观配置。"""

    def get(self, uid):
        return self._load().get(uid, {})

    def get_core(self, uid):
        cfg = self.get(uid)
        core = dict(cfg.get("core") or {})
        core.setdefault("soul_id", "AURA-01")
        core.setdefault("aura", "calm")
        core.setdefault("beat_status", "idle")
        # 主灵魂 = 当前激活角色（单一数据源：读角色会话，不另存）
        core["primary_character"] = self._primary_character(uid)
        return core

    def _primary_character(self, uid):
        try:
            from fuguoai_core.utils.character_store import get_character_router
            sess = get_character_router().sessions.get(uid)
            return sess.character_id if sess else None
        except Exception:
            return None

    def set_diy(self, uid, patch):
        with self._lock:
            db = self._load()
            cfg = db.setdefault(uid, {})
            diy = dict(cfg.get("diy") or {})
            for k in ("color", "form", "motion"):
                if k in patch:
                    diy[k] = patch[k]
            cfg["diy"] = diy
            self._save(db)
        return diy


# ---------------------------------------------------------------------------
# 用户档案 + 偏好（§10）
# ---------------------------------------------------------------------------
class UserProfileStore(_JsonStore):
    """uid → {nickname, avatar, preferences}。"""

    def get(self, uid):
        d = self._load().get(uid, {})
        return {
            "user_id": uid,
            "nickname": d.get("nickname") or "",
            "avatar": d.get("avatar") or "",
        }

    def update(self, uid, patch):
        with self._lock:
            db = self._load()
            d = db.setdefault(uid, {})
            for k in ("nickname", "avatar"):
                if k in patch:
                    d[k] = patch[k]
            self._save(db)
        return self.get(uid)

    def clear_character_override(self, uid, character_id):
        """清掉该 uid 对某角色的自定义覆盖（改名/改人格/改音色）。

        set_preferences 对 character_overrides 是**按 cid 合并**语义（只能增改、删不掉），
        所以"一键清空/转手"必须走这里做真删除。返回是否确有删除。
        """
        with self._lock:
            db = self._load()
            d = db.get(uid)
            if not d:
                return False
            prefs = dict(d.get("preferences") or {})
            ov = prefs.get(_OVERRIDES_KEY)
            if not isinstance(ov, dict) or str(character_id) not in ov:
                return False
            ov = {k: v for k, v in ov.items() if k != str(character_id)}
            prefs[_OVERRIDES_KEY] = ov
            d["preferences"] = prefs
            self._save(db)
            return True

    def get_preferences(self, uid):
        return dict(self._load().get(uid, {}).get("preferences") or {})

    def set_preferences(self, uid, patch):
        if not isinstance(patch, dict):
            raise ValueError("preferences 须为对象")
        with self._lock:
            db = self._load()
            d = db.setdefault(uid, {})
            prefs = dict(d.get("preferences") or {})
            for k, v in patch.items():
                if k == _OVERRIDES_KEY:
                    # 心的资料覆盖：必须按 character_id 合并，绝不能整块替换
                    # （否则写 A 心会把 B 心的覆盖冲掉）。且只收白名单字段，
                    # gender/age/system_prompt 仍归 admin，客户端写不进来。
                    if not isinstance(v, dict):
                        raise ValueError(f"{_OVERRIDES_KEY} 须为对象")
                    cur = dict(prefs.get(_OVERRIDES_KEY) or {})
                    for cid, ov in v.items():
                        if not isinstance(ov, dict):
                            continue
                        merged = dict(cur.get(cid) or {})
                        merged.update({f: ov[f] for f in _OVERRIDE_FIELDS if f in ov})
                        cur[str(cid)] = merged
                    prefs[_OVERRIDES_KEY] = cur
                else:
                    prefs[k] = v
            d["preferences"] = prefs
            self._save(db)
        return prefs


# ---------------------------------------------------------------------------
# 进程级单例（与 character_store 同范式）
# ---------------------------------------------------------------------------
_journal_store = None
_memory_store = None
_home_store = None
_user_store = None
_persona_store = None


def get_journal_store() -> JournalStore:
    global _journal_store
    if _journal_store is None:
        _journal_store = JournalStore(_JOURNALS_PATH)
    return _journal_store


def get_memory_store() -> MemoryStore:
    global _memory_store
    if _memory_store is None:
        _memory_store = MemoryStore(_MEMORIES_PATH)
    return _memory_store


def get_home_store() -> HomeStore:
    global _home_store
    if _home_store is None:
        _home_store = HomeStore(_HOME_PATH)
    return _home_store


def get_user_store() -> UserProfileStore:
    global _user_store
    if _user_store is None:
        _user_store = UserProfileStore(_USERS_PATH)
    return _user_store


def get_persona_store() -> PersonaOffsetStore:
    global _persona_store
    if _persona_store is None:
        _persona_store = PersonaOffsetStore(_PERSONA_PATH)
    return _persona_store
