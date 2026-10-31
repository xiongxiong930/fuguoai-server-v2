"""主动对话引擎（Proactive Engagement）Phase A —— 纯后端 MVP。

设计（见 docs/PROACTIVE_ENGAGEMENT_PLAN.md §3）：
- 触发源（已锁定三类 + 8 项补充建议中的可落地者）：
  ① 时间触发：监听 reminder.fired 事件 → 顺带发起"相关搭话"（复用联网提醒）。
  ② 沉默回归：设备久未互动（默认 3 天）→ 轻量问候。
  ③ 随机惊喜：活跃窗口内随机轻量搭话（非固定点，制造"活人感"）。
  （记忆触发/外接触发属 Phase A+ / 跨团队，本 MVP 不实现，接口预留。）
- 下发：复用留言/提醒同一条 vm-server 虚拟对端通道（固件零改动）。
- 护栏：空闲前置 + 频率≤N + 静默时段 + 24h 去重 + 隐私（仅在线）。
- 跨源仲裁：TriggerArbiter 取最高分候选，避免重复打扰。

红线合规：HTTP 只扩不删改（路由在 proactive_routes）；8000 WS 零改；实时推送走 event_bus；
不碰 chat()/TTS 核心（仅调用 _tts_to_opus）；下发零固件改动。
"""

import asyncio
import json
import logging
import os
import random
import threading
import time

TAG = __name__
_logger = logging.getLogger(TAG)

WORKER_INTERVAL = 15.0          # 扫描轮询间隔（秒）
DORMANCY_SEC = 3 * 86400        # 沉默回归阈值（默认 3 天）
DEFAULT_MAX_PER_DAY = 3         # 每设备每天主动触发上限
RANDOM_P = 0.01                 # 随机触发概率（每扫描，配合"今日未触发"门控 ≈ 每天至多 1 次）
QUIET_DEFAULT = ["22:00", "08:00"]   # 默认勿扰窗口（含跨午夜回绕）

_RANDOM_LINES = [
    "今天天气不错，有什么想聊的吗？",
    "刚有点想你了，要不要听首歌？",
    "忙了一天，记得休息一下哦。",
    "刚好有空，要不要聊聊今天发生的事？",
]


class Candidate:
    """单个触发候选。score 越高越优先；topic 用于 24h 去重。"""
    __slots__ = ("reason", "topic", "score", "text")

    def __init__(self, reason, topic, score, text=None):
        self.reason = reason
        self.topic = topic
        self.score = score
        self.text = text

    def to_dict(self):
        return {"reason": self.reason, "topic": self.topic,
                "score": self.score, "text": self.text}


# ───────────────────────────── 存储 ─────────────────────────────

class ProactiveStore:
    """每设备配置 + 运行状态，JSON 持久化（纯文件，无外部依赖）。"""

    _BASE_DIR = "/opt/fuguoai-server/data/proactive"

    def __init__(self):
        self._lock = threading.Lock()
        self._cfg = {}    # device_id -> dict
        self._state = {}  # device_id -> dict
        try:
            os.makedirs(self._BASE_DIR, exist_ok=True)
        except Exception:
            pass

    # —— 配置 ——
    def get_config(self, device_id):
        with self._lock:
            return dict(self._cfg.get(device_id, self._default_cfg()))

    def set_config(self, device_id, patch: dict):
        with self._lock:
            cur = dict(self._cfg.get(device_id, self._default_cfg()))
            for k, v in patch.items():
                if k in ("enabled", "allow_external"):
                    cur[k] = bool(v)
                elif k == "max_per_day":
                    cur["max_per_day"] = max(1, int(v))
                elif k == "quiet_hours":
                    cur["quiet_hours"] = list(v)[:2]
                elif k == "topics":
                    cur["topics"] = list(v)
                elif k == "triggers" and isinstance(v, dict):
                    cur["triggers"].update({kk: bool(vv) for kk, vv in v.items()})
            self._cfg[device_id] = cur
            self._save(device_id, "cfg", cur)
            return dict(cur)

    @staticmethod
    def _default_cfg():
        return {
            "enabled": False,                 # 默认关闭，需用户开启
            "max_per_day": DEFAULT_MAX_PER_DAY,
            "quiet_hours": list(QUIET_DEFAULT),
            "topics": [],
            "triggers": {"time": True, "silence": True, "random": True},
            "allow_external": False,          # 外接触发（PIR/多模态）默认关，需授权
        }

    # —— 运行状态 ——
    def get_state(self, device_id):
        with self._lock:
            return dict(self._state.get(device_id, self._default_state()))

    def record_activity(self, device_id, ts=None):
        """用户侧活动（如设置提醒）→ 更新 last_user_activity_ts。"""
        ts = ts or int(time.time())
        with self._lock:
            st = dict(self._state.get(device_id, self._default_state()))
            st["last_user_activity_ts"] = ts
            self._state[device_id] = st
            self._save(device_id, "state", st)

    def record_said(self, device_id, cand: Candidate, text, ts=None):
        ts = ts or int(time.time())
        with self._lock:
            st = dict(self._state.get(device_id, self._default_state()))
            st["last_proactive_ts"] = ts
            today = time.strftime("%Y-%m-%d", time.localtime(ts))
            st["daily_count"][today] = st["daily_count"].get(today, 0) + 1
            st["said"].append({"ts": ts, "reason": cand.reason,
                               "topic": cand.topic, "text": text})
            st["said"] = st["said"][-20:]
            self._state[device_id] = st
            self._save(device_id, "state", st)

    @staticmethod
    def _default_state():
        return {"last_proactive_ts": 0, "last_user_activity_ts": 0,
                "daily_count": {}, "said": []}

    # —— 护栏查询 ——
    def daily_count_today(self, device_id, now=None):
        now = now or int(time.time())
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        return self._state.get(device_id, {}).get("daily_count", {}).get(today, 0)

    def said_topic_recently(self, device_id, topic, window_sec=86400, now=None):
        now = now or int(time.time())
        for item in self._state.get(device_id, {}).get("said", []):
            if item.get("topic") == topic and now - item["ts"] < window_sec:
                return True
        return False

    # —— 持久化 ——
    def _path(self, device_id, kind):
        safe = device_id.replace(":", "_").replace("/", "_")
        return os.path.join(self._BASE_DIR, f"{safe}.{kind}.json")

    def _save(self, device_id, kind, data):
        try:
            tmp = self._path(device_id, kind) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self._path(device_id, kind))
        except Exception as e:
            _logger.warning(f"[proactive] 持久化失败 {device_id}/{kind}: {e}")


# ───────────────────────────── 引擎 ─────────────────────────────

class ProactiveEngine:
    def __init__(self):
        self._store = ProactiveStore()
        self._task = None
        self._pending_time = []   # [(device_id, event)] 待处理的 reminder.fired
        self._lock = threading.Lock()
        self._say = None          # 注入式下发函数（测试用）；默认走 vm-server 推流
        self._bus_queue = None    # event_bus 订阅队列（start() 内赋值）

    # ── 配置门面 ──
    def get_config(self, device_id):
        return self._store.get_config(device_id)

    def set_config(self, device_id, patch):
        return self._store.set_config(device_id, patch)

    def note_activity(self, device_id, ts=None):
        self._store.record_activity(device_id, ts)

    def enqueue_time_trigger(self, device_id, event):
        with self._lock:
            self._pending_time.append((device_id, event))

    # ── 触发评估（纯函数，便于离线测试） ──
    def evaluate(self, device_id, now=None):
        """收集该设备当前所有候选触发（不校验护栏），返回 Candidate 列表。"""
        now = now or int(time.time())
        cfg = self._store.get_config(device_id)
        cands = []
        if not cfg.get("enabled", False):
            return cands

        # ① 时间触发：消费 reminder.fired 队列
        if cfg["triggers"].get("time", True):
            with self._lock:
                due = [e for (d, e) in self._pending_time if d == device_id]
            for ev in due:
                rid = ev.get("rid", "x")
                rtext = (ev.get("text") or "")[:30]
                cands.append(Candidate(
                    reason="reminder_related", topic=f"reminder:{rid}",
                    score=0.7,
                    text=f"刚才的提醒「{rtext}」你处理好了吗？需要我帮你记着吗？" if rtext
                    else "刚才那个提醒你处理好了吗？"))

        # ② 沉默回归
        if cfg["triggers"].get("silence", True):
            st = self._store.get_state(device_id)
            last = max(st.get("last_proactive_ts", 0), st.get("last_user_activity_ts", 0))
            if last and (now - last) >= DORMANCY_SEC:
                cands.append(Candidate(
                    reason="silence_regression", topic="silence",
                    score=0.5,
                    text="好几天没和你聊天了，最近一切都还顺利吗？"))

        # ③ 随机惊喜（今日未触发才参与）
        if cfg["triggers"].get("random", True):
            if self._store.daily_count_today(device_id, now) == 0 and random.random() < RANDOM_P:
                cands.append(Candidate(
                    reason="random_surprise", topic="random",
                    score=0.3,
                    text=random.choice(_RANDOM_LINES)))
        return cands

    # ── 护栏 ──
    def _pass_guardrails(self, device_id, cand: Candidate, now=None):
        now = now or int(time.time())
        cfg = self._store.get_config(device_id)
        # 频率
        if self._store.daily_count_today(device_id, now) >= cfg.get("max_per_day", DEFAULT_MAX_PER_DAY):
            return False
        # 静默时段
        if _in_quiet_hours(now, cfg.get("quiet_hours", QUIET_DEFAULT)):
            return False
        # 24h 去重
        if self._store.said_topic_recently(device_id, cand.topic, 86400, now):
            return False
        return True

    # ── 跨源仲裁 ──
    @staticmethod
    def arbitrate(cands):
        """取最高分候选；同分取先入列。无候选返回 None。"""
        if not cands:
            return None
        return max(cands, key=lambda c: c.score)

    # ── 下发（复用联网提醒的 vm-server 推流） ──
    async def proactive_say(self, device_id, text):
        if self._say is not None:
            return await self._say(device_id, text)
        try:
            from fuguoai_core.utils.networked_reminder_scheduler import (
                _tts_to_opus, _stream_opus_to_device)
        except Exception as e:
            _logger.warning(f"[proactive] 推流组件导入失败: {e}")
            return False
        opus = await _tts_to_opus(text)
        if not opus:
            return False
        return await _stream_opus_to_device(device_id, opus, label="proactive")

    async def fire(self, device_id, cand: Candidate):
        text = cand.text or f"（{cand.reason}）"
        ok = await self.proactive_say(device_id, text)
        if ok:
            self._store.record_said(device_id, cand, text)
            self._publish(device_id, text, cand)
        return ok

    def _publish(self, device_id, text, cand: Candidate):
        try:
            from fuguoai_core.utils.event_bus import get_event_bus
            get_event_bus().publish({
                "type": "proactive.said",
                "device_id": device_id,
                "text": text,
                "trigger": cand.reason,
                "topic": cand.topic,
                "ts": int(time.time()),
            })
        except Exception:
            pass

    # ── Worker ──
    async def _scan_once(self):
        # 消费 reminder.fired 事件（时间触发）
        if self._bus_queue is not None:
            while not self._bus_queue.empty():
                try:
                    ev = self._bus_queue.get_nowait()
                except Exception:
                    break
                if isinstance(ev, dict) and ev.get("type") == "reminder.fired":
                    self.enqueue_time_trigger(ev.get("device_id"), ev)

        # 枚举设备
        devices = self._list_devices()
        for device_id in devices:
            cfg = self._store.get_config(device_id)
            if not cfg.get("enabled", False):
                continue
            if not _device_playable(device_id):
                continue
            cands = self.evaluate(device_id)
            best = self.arbitrate(cands)
            if best and self._pass_guardrails(device_id, best):
                try:
                    await self.fire(device_id, best)
                except Exception as e:
                    _logger.warning(f"[proactive] fire 异常(设备 {device_id}): {e}")
            # 清掉已消费的时间触发（避免同一提醒重复触发）
            with self._lock:
                self._pending_time = [(d, e) for (d, e) in self._pending_time if d != device_id]

    def _list_devices(self):
        try:
            from fuguoai_core.device_access import get_device_binding_store
            return [d.get("device_id") for d in get_device_binding_store().list_all()
                    if d.get("device_id")]
        except Exception:
            return []

    async def _worker(self):
        while True:
            try:
                await asyncio.sleep(WORKER_INTERVAL)
                await self._scan_once()
            except asyncio.CancelledError:
                return
            except Exception as e:
                _logger.warning(f"[proactive] worker 异常(继续): {e}")

    def ensure_worker(self):
        if self._task is None or self._task.done():
            try:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = asyncio.get_event_loop()
                self._task = loop.create_task(self._worker())
            except Exception:
                self._task = None

    def start(self):
        """生产启动：订阅 event_bus（消费 reminder.fired）+ 起 worker。须在 asyncio loop 内调用。"""
        try:
            from fuguoai_core.utils.event_bus import get_event_bus
            _sid, self._bus_queue = get_event_bus().subscribe()
        except Exception as e:
            _logger.warning(f"[proactive] event_bus 订阅失败(时间触发不可用): {e}")
        self.ensure_worker()


# ───────────────────────────── 工具 ─────────────────────────────

def _device_playable(device_id):
    """在线 + 不在对讲态 + 不在活跃呼叫单（与留言/提醒同款）。"""
    try:
        from fuguoai_core.utils.networked_reminder_scheduler import _device_playable as _dp
        return _dp(device_id)
    except Exception:
        return False


def _parse_hhmm(s):
    try:
        h, m = s.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return 0


def _in_quiet_hours(now, quiet):
    """quiet=[start,end] 字符串 'HH:MM'；支持跨午夜回绕（如 22:00-08:00）。"""
    try:
        start = _parse_hhmm(quiet[0])
        end = _parse_hhmm(quiet[1])
    except Exception:
        return False
    cur = time.localtime(now)
    cur_m = cur.tm_hour * 60 + cur.tm_min
    if start == end:
        return False
    if start < end:
        return start <= cur_m <= end
    return cur_m >= start or cur_m <= end  # 跨午夜


_ENGINE = None


def get_proactive_engine() -> ProactiveEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = ProactiveEngine()
    return _ENGINE
