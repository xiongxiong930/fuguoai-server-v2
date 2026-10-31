"""设备连接风暴熔断（per-device 滑动窗口 + 冷却期）。

背景（2026-08-01 e268 实测）：设备固件崩溃后进入 boot loop，每 2.95s 建立一次
WS 连接、跑完 hello/IoT 上报后崩溃再连，持续 9 分钟约 180 次。每一次连接后端都要
完整初始化一套 LLM + ASR + TTS + 5980 字增强提示词（``_background_initialize``），
5 分钟白跑 100 次，CPU / 日志 / 上游 API 配额全部空转。

本模块提供一个**极保守**的熔断器：仅在单设备建连频率明显超出任何正常场景时才
短暂拒绝，并在冷却期结束后自动放行（自愈），绝不永久封禁、绝不封 IP。

设计要点
--------
- **判据只看建连频率**，不看连接内行为——崩溃循环的连接在后端侧一切正常
  （hello/IoT 都跑完），按"失败连接"统计反而抓不到。
- **阈值留足余量**：默认 60s 内 15 次。正常设备重连（断网恢复 / 服务端重启 /
  用户手动重启）远达不到；e268 崩溃循环是 ~20 次/分。
- **冷却期短**（默认 60s）且到期即清零重新观察：若设备已自愈，最多被挡 60s；
  若仍在风暴，则再次熔断，形成"挡 60s → 放行观察 → 再挡"的有界节流。
- **设备信誉配额（自进化⑥）**：频繁触发熔断的设备信誉下降 → 配额收紧
  （effective_max 下调）+ 冷却缩短（快速识别"在打补丁循环"的设备，避免它
  刷爆建连把正常设备也拖进冷却）；观察窗口内未再触发则信誉缓升回 1.0。
  信誉只影响该设备自身阈值，绝不波及正常设备。
- **拒绝走标准 WS 关闭码 1013(Try Again Later)**，不发任何自定义消息，
  不改设备 8000 WS 协议格式。
- **日志限频**：熔断触发打一条 WARNING，冷却期内的拒绝每 10s 最多一条，
  避免熔断器自己变成新的刷屏源。真实计数一律走 metrics。

可观测（/metrics）
------------------
- ``fuguoai_conn_storm_tripped_total{device_id}``  熔断触发次数
- ``fuguoai_conn_storm_rejected_total{device_id}`` 冷却期内被拒连接数
"""
from __future__ import annotations

import logging
import threading
import time

from fuguoai_core.utils.metrics import METRICS

TAG = __name__
_logger = logging.getLogger(TAG)

# 默认阈值（可经 config["conn_storm_guard"] 覆盖）
WINDOW_SEC = 60.0        # 滑动窗口长度
MAX_CONN_PER_WINDOW = 15  # 窗口内允许的最大建连次数（含本次）
COOLDOWN_SEC = 60.0      # 触发后的冷却时长
_LOG_INTERVAL_SEC = 10.0  # 冷却期内拒绝日志的最小间隔


class ConnectionStormGuard:
    """per-device 建连频率熔断器（线程安全，纯内存、无 IO）。"""

    def __init__(
        self,
        window_sec: float = WINDOW_SEC,
        max_conn: int = MAX_CONN_PER_WINDOW,
        cooldown_sec: float = COOLDOWN_SEC,
        enabled: bool = True,
        exempt_devices: list[str] | None = None,
    ):
        self._lock = threading.Lock()
        self._window = float(window_sec)
        self._max = int(max_conn)
        self._cooldown = float(cooldown_sec)
        self._enabled = bool(enabled)
        self._exempt = set(exempt_devices or ())
        self._hist: dict[str, list[float]] = {}          # device_id -> 建连时刻列表
        self._cooldown_until: dict[str, float] = {}      # device_id -> 冷却截止
        self._last_log: dict[str, float] = {}            # device_id -> 上次打日志时刻
        self._suppressed: dict[str, int] = {}            # device_id -> 被抑制日志条数
        # 自进化⑥：设备信誉（1.0=正常，越低越可疑）。频繁熔断→降；观察期安静→缓升。
        self._reputation: dict[str, float] = {}
        self._rep_decay = 0.7        # 每次熔断信誉乘子（下限 _REP_FLOOR）
        self._rep_recover = 0.05     # 每个干净窗口信誉回血
        self._rep_floor = 0.2        # 信誉下限（配额不会低于 _max*0.2）
        self._rep_cooldown_floor = 5.0  # 冷却下限秒（信誉低→冷却短，快速识别风暴）

    def configure(self, cfg: dict | None):
        """按配置覆盖阈值与豁免名单（幂等；非法值忽略，保持当前值）。"""
        if not cfg:
            return
        with self._lock:
            try:
                self._enabled = bool(cfg.get("enabled", self._enabled))
                self._exempt = set(cfg.get("exempt_devices", list(self._exempt)))
            except (TypeError, ValueError):
                pass
            try:
                self._window = max(1.0, float(cfg.get("window_sec", self._window)))
                self._max = max(1, int(cfg.get("max_conn", self._max)))
                self._cooldown = max(1.0, float(cfg.get("cooldown_sec", self._cooldown)))
            except (TypeError, ValueError):
                pass

    def allow(self, device_id: str | None) -> tuple[bool, str]:
        """登记一次建连尝试并判定是否放行。

        Returns:
            (allowed, reason)。``allowed=False`` 时 reason 为简短英文原因，
            供调用方写日志 / 关闭原因，不下发给设备。

        说明：device_id 为空（如未知设备 / phone 端）一律放行——熔断只针对
        能明确归属的设备，避免把无法归因的连接误伤。
        """
        if not device_id:
            return True, ""
        if not self._enabled:
            return True, ""
        if device_id in self._exempt:
            return True, ""
        now = time.time()
        with self._lock:
            until = self._cooldown_until.get(device_id, 0.0)
            if now < until:
                # 冷却中：直接拒绝，不登记本次（避免冷却期把窗口撑满导致永久拒绝）
                METRICS.inc(
                    "fuguoai_conn_storm_rejected_total", 1.0,
                    labels={"device_id": device_id},
                    help_text="连接风暴冷却期内被拒绝的建连数",
                )
                self._suppressed[device_id] = self._suppressed.get(device_id, 0) + 1
                if now - self._last_log.get(device_id, 0.0) >= _LOG_INTERVAL_SEC:
                    n = self._suppressed.pop(device_id, 0)
                    self._last_log[device_id] = now
                    _logger.warning(
                        f"[conn_guard] {device_id} 连接风暴冷却中，已拒绝 {n} 次建连，"
                        f"剩余 {until - now:.0f}s（设备侧疑似崩溃重连循环）"
                    )
                return False, f"storm_cooldown remain={until - now:.0f}s"

            if until:
                # 冷却刚结束：清空历史，给一个干净窗口重新观察（自愈放行）
                self._cooldown_until.pop(device_id, None)
                self._hist.pop(device_id, None)
                self._last_log.pop(device_id, None)
                self._suppressed.pop(device_id, None)
                # 信誉回血：安静度过一个冷却期 → 缓升（上限 1.0）
                rep = self._reputation.get(device_id, 1.0)
                if rep < 1.0:
                    self._reputation[device_id] = min(1.0, rep + self._rep_recover)

            hist = [t for t in self._hist.get(device_id, ()) if now - t <= self._window]
            hist.append(now)
            self._hist[device_id] = hist

            # 自进化⑥：按信誉算 effective 阈值——信誉低→配额收紧、但冷却缩短
            # （快速识别"在打补丁循环"的设备，避免它长期阻塞把正常设备也拖进冷却）。
            rep = self._reputation.get(device_id, 1.0)
            eff_max = max(2, int(self._max * rep))
            eff_cooldown = max(self._rep_cooldown_floor, self._cooldown * rep)
            if len(hist) > eff_max:
                self._cooldown_until[device_id] = now + eff_cooldown
                self._hist.pop(device_id, None)
                # 信誉衰减：频繁熔断 → 降（下限 _rep_floor）
                self._reputation[device_id] = max(self._rep_floor, rep * self._rep_decay)
                METRICS.inc(
                    "fuguoai_conn_storm_tripped_total", 1.0,
                    labels={"device_id": device_id},
                    help_text="连接风暴熔断触发次数",
                )
                METRICS.inc(
                    "fuguoai_conn_storm_rejected_total", 1.0,
                    labels={"device_id": device_id},
                    help_text="连接风暴冷却期内被拒绝的建连数",
                )
                METRICS.set_gauge(
                    "fuguoai_conn_storm_reputation", rep,
                    labels={"device_id": device_id},
                    help_text="设备连接信誉(1.0正常,越低越频繁风暴)",
                )
                self._last_log[device_id] = now
                _logger.warning(
                    f"[conn_guard] {device_id} 触发连接风暴熔断："
                    f"{self._window:.0f}s 内建连 {len(hist)} 次(阈值 {eff_max}, "
                    f"信誉 {rep:.2f})，冷却 {eff_cooldown:.0f}s 内拒绝新连接以避免重复初始化"
                )
                return False, "storm_tripped"

            return True, ""

    def snapshot(self) -> dict:
        """排障用只读快照。"""
        now = time.time()
        with self._lock:
            return {
                "window_sec": self._window,
                "max_conn": self._max,
                "cooldown_sec": self._cooldown,
                "tracked": {
                    d: len([t for t in ts if now - t <= self._window])
                    for d, ts in self._hist.items()
                },
                "cooling": {
                    d: round(u - now, 1)
                    for d, u in self._cooldown_until.items() if u > now
                },
                "reputation": {
                    d: round(r, 2) for d, r in self._reputation.items() if r < 1.0
                },
            }

    def reset(self, device_id: str | None = None):
        """清空状态（设备恢复后手动放行 / 测试用）。"""
        with self._lock:
            if device_id is None:
                self._hist.clear()
                self._cooldown_until.clear()
                self._last_log.clear()
                self._suppressed.clear()
            else:
                self._hist.pop(device_id, None)
                self._cooldown_until.pop(device_id, None)
                self._last_log.pop(device_id, None)
                self._suppressed.pop(device_id, None)


_guard: ConnectionStormGuard | None = None
_guard_lock = threading.Lock()


def get_conn_storm_guard() -> ConnectionStormGuard:
    global _guard
    if _guard is None:
        with _guard_lock:
            if _guard is None:
                _guard = ConnectionStormGuard()
    return _guard
