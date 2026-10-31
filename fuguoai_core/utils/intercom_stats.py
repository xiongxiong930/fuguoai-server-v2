"""对讲通话质量指标采集（docs/INTERCOM_STATS_DESIGN.md v0.1 落地）。

把 07-28 联调期「flag+落盘定罪」的诊断资产产品化为常驻轻量指标：
per-call 帧计数、6×10s 滑窗 fps、编解码错误、断流(stall)判定。

设计约束（红线自查）
- 纯内存、零持久化；进程重启即清零。
- 采集方法全部内部 try/except 自吞——任何异常绝不外溢到 relay 热路径。
- 单把轻量锁，临界区只有 dict/deque 操作，无 IO。
- 会话结束快照保留最近 RECENT_KEEP 通（deque 自动淘汰）。

方向语义（与设计文档一致）
- up   = 手机→设备（60ms 事故的定罪方向，期望 50fps）
- down = 设备→手机（设备上行健康度，期望 ~50fps）
- 设备↔设备会话：a→b 记 up，b→a 记 down（a=发起端）。
"""

import threading
import time
from collections import deque

RECENT_KEEP = 20        # 终态快照保留通数
BUCKET_SEC = 10         # 滑窗桶宽（秒）
BUCKET_KEEP = 6         # 保留桶数（6×10s = 最近 1 分钟）
STALL_SEC = 3.0         # 距最后一帧超过该秒数即判 stall


class _DirStats:
    """单方向计数器（无锁——由 Hub 的锁保护）。"""

    __slots__ = ("frames", "bytes", "errs", "last_ts", "buckets")

    def __init__(self):
        self.frames = 0
        self.bytes = 0
        self.errs = 0
        self.last_ts = 0.0
        # deque[(bucket_id, count)]，bucket_id = int(now // BUCKET_SEC)
        self.buckets = deque(maxlen=BUCKET_KEEP + 1)

    def note(self, n: int, nbytes: int, now: float):
        self.frames += n
        self.bytes += nbytes
        self.last_ts = now
        bid = int(now // BUCKET_SEC)
        if self.buckets and self.buckets[-1][0] == bid:
            last_bid, cnt = self.buckets[-1]
            self.buckets[-1] = (last_bid, cnt + n)
        else:
            self.buckets.append((bid, n))

    def fps_windows(self, now: float):
        """最近 BUCKET_KEEP 个 10s 桶的帧率（当前未满桶按已过时长折算）。"""
        out = []
        cur_bid = int(now // BUCKET_SEC)
        for bid, cnt in self.buckets:
            if bid == cur_bid:
                elapsed = now - bid * BUCKET_SEC
                if elapsed >= 1.0:
                    out.append(round(cnt / elapsed, 1))
            else:
                out.append(round(cnt / BUCKET_SEC, 1))
        return out[-BUCKET_KEEP:]

    def stall(self, now: float) -> bool:
        return bool(self.last_ts) and (now - self.last_ts) > STALL_SEC

    def snap(self, now: float) -> dict:
        return {
            "frames": self.frames,
            "bytes": self.bytes,
            "errs": self.errs,
            "fps_10s": self.fps_windows(now),
            "stall": self.stall(now),
        }


class _CallStats:
    __slots__ = ("sid", "a", "b", "mode", "started_at", "ended_at",
                 "end_reason", "up", "down")

    def __init__(self, sid, a, b, mode):
        self.sid = sid
        self.a = a
        self.b = b
        self.mode = mode
        self.started_at = time.time()
        self.ended_at = None
        self.end_reason = None
        self.up = _DirStats()
        self.down = _DirStats()

    def direction_for(self, from_id: str) -> "_DirStats":
        """方向判定：phone 发出=up；发往 phone=down；设备↔设备 a→b=up。"""
        if from_id.startswith("phone-"):
            return self.up
        other = self.b if from_id == self.a else self.a
        if other.startswith("phone-"):
            return self.down
        return self.up if from_id == self.a else self.down

    def snap(self, now: float = None) -> dict:
        now = now or time.time()
        end = self.ended_at or now
        d = {
            "sid": self.sid,
            "a": self.a,
            "b": self.b,
            "mode": self.mode,
            "started_at": int(self.started_at),
            "age_s": round(end - self.started_at, 1),
            "up": self.up.snap(now),
            "down": self.down.snap(now),
        }
        # 实时率：up 方向转发音频时长 / 会话墙钟（≈1.0=零堆积；1帧=20ms）
        wall = max(end - self.started_at, 0.001)
        d["rt_ratio"] = round(self.up.frames * 0.02 / wall, 2)
        if self.ended_at:
            d["ended_at"] = int(self.ended_at)
            d["end_reason"] = self.end_reason
        return d


class IntercomStatsHub:
    def __init__(self):
        self._lock = threading.Lock()
        self._by_sid = {}    # sid -> _CallStats
        self._by_dev = {}    # device_id -> sid
        self._recent = deque(maxlen=RECENT_KEEP)

    # ── 会话生命周期（intercom_registry 挂钩） ──────────────

    def session_start(self, sid: str, a: str, b: str):
        try:
            mode = ("phone-device"
                    if (a.startswith("phone-") or b.startswith("phone-"))
                    else "device-device")
            with self._lock:
                self._by_sid[sid] = _CallStats(sid, a, b, mode)
                self._by_dev[a] = sid
                self._by_dev[b] = sid
        except Exception:
            pass

    def session_end(self, sid: str, reason: str = None):
        try:
            with self._lock:
                st = self._by_sid.pop(sid, None)
                if not st:
                    return
                for dev in (st.a, st.b):
                    if self._by_dev.get(dev) == sid:
                        self._by_dev.pop(dev, None)
                st.ended_at = time.time()
                st.end_reason = reason
                self._recent.append(st.snap(st.ended_at))
        except Exception:
            pass

    # ── 热路径计数（全部自吞异常） ──────────────────────────

    def note_frames(self, from_id: str, n: int, nbytes: int):
        """n 个 20ms 帧成功转发（手机→设备一次 relay 可含多帧）。"""
        try:
            now = time.time()
            with self._lock:
                sid = self._by_dev.get(from_id)
                st = self._by_sid.get(sid) if sid else None
                if st:
                    st.direction_for(from_id).note(n, nbytes, now)
        except Exception:
            pass

    def note_err(self, from_id: str):
        """编/解码失败 +1（方向与 note_frames 同判）。"""
        try:
            with self._lock:
                sid = self._by_dev.get(from_id)
                st = self._by_sid.get(sid) if sid else None
                if st:
                    st.direction_for(from_id).errs += 1
        except Exception:
            pass

    # ── 查询 ────────────────────────────────────────────────

    def active(self) -> list:
        with self._lock:
            sts = list(self._by_sid.values())
        now = time.time()
        return [st.snap(now) for st in sts]

    def recent(self, limit: int = RECENT_KEEP) -> list:
        with self._lock:
            items = list(self._recent)
        return items[-max(1, min(limit, RECENT_KEEP)):][::-1]


_HUB = None


def get_stats_hub() -> IntercomStatsHub:
    global _HUB
    if _HUB is None:
        _HUB = IntercomStatsHub()
    return _HUB
