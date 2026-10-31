"""联网定时提醒存储（服务端为定时器，到点生成 TTS 推设备播放，固件零改动）。

与离线闹钟(reminder_store.py: 设备本地定时 + 服务端镜像)区分：本模块是
「联网版」——调度存于云端，由本服务在 fire_at 触发 TTS→推流。
持久化到 data/networked_reminders.json（host volume, 重启不丢）。

调度类型(schedule):
  - once:    {"type":"once","fire_at":<unix秒>}                       一次性
  - once:    {"type":"once","in_minutes":30} / {"in_seconds":1800}    相对时间（优先，见下）
  - daily:   {"type":"daily","hour":8,"minute":0}                    每天
  - weekly:  {"type":"weekly","hour":8,"minute":0,"repeat":[0,2,4]}  每周几(0=周一..6=周日)
  - interval:{"type":"interval","every_seconds":1800}                每隔 N 秒

2026-10-18 once 双重加固（生产实证：LLM 自算 unix 时间戳错年份，8818 上
fire_at 被算成一年前 → 创建后立即触发、设备日程又不可见）：
  1) add() 把 in_seconds/in_minutes 相对时间归一成 fire_at（相对时间是 LLM
     唯一算不错的时间表达；与 fire_at 同时给出时相对字段胜出）；
  2) 绝对 fire_at 必须不早于 now-60s（ONCE_FIRE_GRACE_SEC），更早一律拒收，
     错误信息带当前北京时间供调用方自我纠正。
"""

import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

_DATA_DIR = os.environ.get("FUGUOAI_DATA_DIR", "/opt/fuguoai-server/data")
_PATH = os.path.join(_DATA_DIR, "networked_reminders.json")
_lock = threading.Lock()

MAX_TEXT = 200
MAX_PER_DEVICE = 50

# 重试门限（避免设备忙时狂轮询 / TTS 故障时不打爆上游）
DEVICE_RETRY_GAP = 15        # 设备忙/离线：至少隔 15s 再试
TTS_ERROR_COOLDOWN = 300     # TTS 失败：冷却 5 分钟再试

# interval 类提醒最小间隔。2026-10-17 复盘 8818「站立活动一下」every_seconds=180
# 无人看管连响 11.8 天 / 1388 次后，从 60s 上调至 900s（与「站起来/喝水」类
# 健康提醒的实际语义匹配；更短周期请用 daily/weekly 精确到分）。
MIN_INTERVAL_SEC = 900

# once 绝对 fire_at 的过去宽限：容忍时钟偏差与"现在就提醒"（ fire_at 略早于
# now 仍受理并立即触发）；比这更早的一律拒收——LLM 算错年份/日期时会造出
# 立即触发的幽灵提醒（2026-10-18 8818 实证）。
ONCE_FIRE_GRACE_SEC = 60

_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _now():
    return int(time.time())


def _normalize_repeat(repeat):
    out = set()
    for r in (repeat or []):
        if isinstance(r, int) and 0 <= r <= 6:
            out.add(r)
        elif isinstance(r, str):
            key = r.strip().lower()[:3]
            if key in _WEEKDAYS:
                out.add(_WEEKDAYS[key])
    return out


def _compute_next(schedule, now, last_fired_at=None):
    """返回下次触发 unix 秒；无法计算返回 None。"""
    typ = (schedule or {}).get("type")
    if typ == "once":
        fa = schedule.get("fire_at")
        return int(fa) if isinstance(fa, (int, float)) and fa > 0 else None
    if typ == "daily":
        h = int(schedule.get("hour", 0))
        m = int(schedule.get("minute", 0))
        base = datetime.fromtimestamp(now).replace(hour=h, minute=m, second=0, microsecond=0)
        if base.timestamp() <= now:
            base = base + timedelta(days=1)
        return int(base.timestamp())
    if typ == "weekly":
        h = int(schedule.get("hour", 0))
        m = int(schedule.get("minute", 0))
        rep = _normalize_repeat(schedule.get("repeat"))
        if not rep:
            return None
        for d in range(0, 14):
            cand = datetime.fromtimestamp(now) + timedelta(days=d)
            if cand.weekday() in rep:
                c = cand.replace(hour=h, minute=m, second=0, microsecond=0)
                if c.timestamp() > now:
                    return int(c.timestamp())
        return None
    if typ == "interval":
        every = int(schedule.get("every_seconds", 0))
        if every < MIN_INTERVAL_SEC:
            return None
        base = last_fired_at or now
        return int(base + every)
    return None


def _once_relative_seconds(schedule):
    """once 相对时间(in_seconds/in_minutes) → 总秒数；无相对字段返回 0。"""
    total = 0
    for key, mult in (("in_seconds", 1), ("in_minutes", 60)):
        v = (schedule or {}).get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            total += int(v * mult)
    return total


def _normalize_once(schedule, now):
    """把 once 的相对时间字段就地归一成绝对 fire_at。返回是否发生了归一化。

    LLM 直接推算 unix 时间戳不可靠（会把年份算错），相对时间是唯一稳妥的
    表达；fire_at 与相对字段同时给出时相对字段胜出。
    """
    if not isinstance(schedule, dict) or schedule.get("type") != "once":
        return False
    total = _once_relative_seconds(schedule)
    if total <= 0:
        return False
    schedule.pop("in_seconds", None)
    schedule.pop("in_minutes", None)
    schedule["fire_at"] = int(now) + total
    return True


def _validate_schedule(schedule, now=None):
    """返回 (ok, error)。now 缺省取真实时钟（测试可传入固定基准）。"""
    if not isinstance(schedule, dict):
        return False, "schedule 必须是对象"
    typ = schedule.get("type")
    if typ not in ("once", "daily", "weekly", "interval"):
        return False, "schedule.type 必须是 once/daily/weekly/interval"
    if typ == "once":
        if _once_relative_seconds(schedule) > 0:
            pass  # 相对时间由 add() 归一化成未来 fire_at，天然合法
        else:
            fa = schedule.get("fire_at")
            if not isinstance(fa, (int, float)) or fa <= 0:
                return False, "once 需要 fire_at(未来unix秒) 或 in_seconds/in_minutes(相对时间)"
            now = now if now is not None else _now()
            if fa < now - ONCE_FIRE_GRACE_SEC:
                bj = datetime.fromtimestamp(
                    int(now), tz=timezone(timedelta(hours=8))
                ).strftime("%Y-%m-%d %H:%M:%S")
                return False, (
                    f"once 的 fire_at={int(fa)} 已过去（当前北京时间 {bj}）。"
                    "请改用 in_seconds/in_minutes 相对时间，或按当前时间重新计算 fire_at"
                )
    elif typ in ("daily", "weekly"):
        h = schedule.get("hour")
        m = schedule.get("minute")
        if not isinstance(h, int) or not (0 <= h <= 23):
            return False, "hour 必须 0-23 整数"
        if not isinstance(m, int) or not (0 <= m <= 59):
            return False, "minute 必须 0-59 整数"
        if typ == "weekly" and not _normalize_repeat(schedule.get("repeat")):
            return False, "weekly 需要 repeat(0-6 或 mon..sun 列表)"
    elif typ == "interval":
        every = schedule.get("every_seconds")
        if not isinstance(every, int) or every < MIN_INTERVAL_SEC:
            return False, f"interval 需要 every_seconds>={MIN_INTERVAL_SEC}"
    return True, None


class NetworkedReminderStore:
    """联网提醒的持久化与调度计算。线程安全（单进程 asyncio，锁仅防并发文件写）。"""

    def __init__(self, path=None):
        self._path = path or _PATH
        self._lock = threading.Lock()

    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self, db):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    def add(self, device_id, uid, text, schedule, now=None):
        """创建联网提醒。返回 (rid, error)；失败 (None, 原因)。"""
        text = (text or "").strip()
        if not text:
            return None, "text 不能为空"
        if len(text) > MAX_TEXT:
            return None, f"text 超过 {MAX_TEXT} 字"
        now = now or _now()
        # 拷贝后归一化：once 相对时间 → 绝对 fire_at；调用方原始 dict 不被改写
        schedule = dict(schedule) if isinstance(schedule, dict) else schedule
        _normalize_once(schedule, now)
        ok, err = _validate_schedule(schedule, now=now)
        if not ok:
            return None, err
        with self._lock:
            db = self._load()
            items = db.get(device_id, [])
            if len([it for it in items if it.get("status") in ("pending", "disabled")]) >= MAX_PER_DEVICE:
                return None, f"单设备最多 {MAX_PER_DEVICE} 条联网提醒"
            nxt = _compute_next(schedule, now)
            rid = "nr" + uuid.uuid4().hex[:12]
            item = {
                "rid": rid, "device_id": device_id, "uid": uid, "text": text,
                "schedule": schedule, "status": "pending", "created_at": now,
                "next_fire_at": nxt, "last_fired_at": None, "fire_count": 0,
                "last_attempt_at": None, "last_error_at": None, "last_error": None,
            }
            items.append(item)
            db[device_id] = items
            self._save(db)
        return rid, None

    def get(self, device_id, rid):
        db = self._load()
        for it in db.get(device_id, []):
            if it.get("rid") == rid:
                return it
        return None

    def list(self, device_id, include_disabled=False):
        db = self._load()
        items = [it for it in db.get(device_id, [])
                 if include_disabled or it.get("status") != "disabled"]
        items.sort(key=lambda x: (x.get("next_fire_at") or (1 << 62), x.get("created_at") or 0))
        return items

    def cancel(self, device_id, rid):
        with self._lock:
            db = self._load()
            items = db.get(device_id, [])
            before = len(items)
            items = [it for it in items if it.get("rid") != rid]
            if len(items) == before:
                return False
            db[device_id] = items
            self._save(db)
        return True

    def set_enabled(self, device_id, rid, enabled):
        with self._lock:
            db = self._load()
            for it in db.get(device_id, []):
                if it.get("rid") == rid:
                    if enabled:
                        if it.get("status") == "disabled":
                            it["status"] = "pending"
                            if it.get("next_fire_at") is None and it.get("schedule", {}).get("type") != "once":
                                it["next_fire_at"] = _compute_next(it["schedule"], _now())
                    else:
                        it["status"] = "disabled"
                    self._save(db)
                    return True
        return False

    def mark_fired(self, device_id, rid, now=None):
        now = now or _now()
        with self._lock:
            db = self._load()
            for it in db.get(device_id, []):
                if it.get("rid") == rid:
                    it["last_fired_at"] = now
                    it["fire_count"] = it.get("fire_count", 0) + 1
                    if it.get("schedule", {}).get("type") == "once":
                        it["status"] = "fired"
                        it["next_fire_at"] = None
                    else:
                        it["status"] = "pending"
                        it["next_fire_at"] = _compute_next(it["schedule"], now, last_fired_at=now)
                        it["last_attempt_at"] = None
                        it["last_error_at"] = None
                    self._save(db)
                    return True
        return False

    def set_attempt(self, device_id, rid, now, kind, error=None):
        """记录一次失败尝试，供 pending_due 做重试门限。"""
        with self._lock:
            db = self._load()
            for it in db.get(device_id, []):
                if it.get("rid") == rid:
                    if kind == "device":
                        it["last_attempt_at"] = now
                    elif kind == "tts":
                        it["last_error_at"] = now
                        it["last_error"] = (error or "")[:200]
                    self._save(db)
                    return True
        return False

    def pending_due(self, now, window=2):
        """返回待触发(状态 pending 且到点)且未被重试门限拦截的提醒（按 next_fire_at 升序）。"""
        out = []
        db = self._load()
        for items in db.values():
            for it in items:
                if it.get("status") != "pending":
                    continue
                nxt = it.get("next_fire_at")
                if not nxt or nxt > now + window:
                    continue
                if it.get("last_attempt_at") and now - it["last_attempt_at"] < DEVICE_RETRY_GAP:
                    continue
                if it.get("last_error_at") and now - it["last_error_at"] < TTS_ERROR_COOLDOWN:
                    continue
                out.append(it)
        out.sort(key=lambda x: x.get("next_fire_at") or 0)
        return out

    def count_active(self, device_id):
        db = self._load()
        return len([it for it in db.get(device_id, []) if it.get("status") in ("pending", "disabled")])


_store = None


def get_networked_reminder_store():
    global _store
    if _store is None:
        _store = NetworkedReminderStore()
    return _store
