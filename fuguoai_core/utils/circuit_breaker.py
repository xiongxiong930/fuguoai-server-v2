"""复果AI · 依赖弹性层：三态熔断器 + 退避重试（自研重构版）。

外部依赖（LLM / TTS / ASR 等远程服务）的故障需要被「看见」且「自愈」。本模块提供：

- ``CircuitBreaker``：CLOSED → OPEN → HALF_OPEN 三态机。
  * CLOSED  正常放行；连续失败达到阈值转 OPEN。
  * OPEN    直接抛 ``CircuitOpenError`` 快速失败（不打远程，阻断雪崩）；
            经过 ``recovery_timeout`` 后自动转 HALF_OPEN。
  * HALF_OPEN 放行少量探测；成功则回 CLOSED（恢复），失败则回 OPEN 重新计时。
  状态实时写 metrics（gauge：0=闭合 1=开路 2=半开），供 /metrics 抓取、/healthz 汇总，
  实现「单点故障 N 秒内切降级、自动恢复」的可观测闭环。

- ``retry_with_backoff``：指数退避 + **去相关抖动（decorrelated jitter）**，打散重试共振，
  比裸指数退避更抗「重试风暴」。

线程安全：状态用一把 ``RLock`` 保护；本结构是同步实现，可直接用于 TTS 音频线程内的
``asyncio.run`` 场景，也可在事件循环线程里调用（状态迁移极轻量）。

设计注释：相比 fork 起点的实现，本次重构只动了内部结构（状态迁移收口为单一
``_transition``、``state`` 只读不重复触发半开判断、退避加入抖动、新增 ``reset``），
对外契约（类名 / 方法签名 / 指标名 / 状态字符串）完全不变，现有回归测试无需改动。
"""

import random
import threading
import time

from fuguoai_core.utils.metrics import METRICS

# 三态常量（对外契约，禁止改名）
CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"

# 状态 → 指标 gauge 取值（对外契约，禁止改值）
_STATE_GAUGE = {CLOSED: 0, OPEN: 1, HALF_OPEN: 2}

# 全局注册表：name -> CircuitBreaker，便于 /healthz 汇总全部熔断器
_REGISTRY: "dict[str, CircuitBreaker]" = {}
_REGISTRY_LOCK = threading.Lock()


class CircuitOpenError(Exception):
    """熔断开路时的快速失败异常；携带熔断名便于排查。"""

    def __init__(self, name: str):
        super().__init__(f"circuit '{name}' is OPEN")
        self.name = name


class CircuitBreaker:
    """三态熔断器。同步实现，线程安全（内部 RLock）。"""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 1,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls

        self._lock = threading.RLock()
        self._state = CLOSED
        self._fail_count = 0
        self._opened_at = 0.0
        self._half_open_calls = 0
        self._publish()

    # ---------------- 状态读取 ----------------
    @property
    def state(self) -> str:
        """当前状态；读取时顺带推进「OPEN→HALF_OPEN」的自动恢复。"""
        with self._lock:
            self._maybe_recover()
            return self._state

    def snapshot(self) -> dict:
        """返回一份状态快照（供 /healthz 汇总）。"""
        with self._lock:
            self._maybe_recover()
            return {
                "name": self.name,
                "state": self._state,
                "fail_count": self._fail_count,
                "recovery_timeout": self.recovery_timeout,
            }

    # ---------------- 状态迁移（收口为单一入口）----------------
    def _maybe_recover(self) -> None:
        """OPEN 超过 recovery_timeout 后转 HALF_OPEN（仅在此处推进恢复）。"""
        if self._state == OPEN and (time.monotonic() - self._opened_at) >= self.recovery_timeout:
            self._transition(HALF_OPEN)

    def _transition(self, new_state: str) -> None:
        """统一状态迁移 + 指标发布；调用方须持 ``_lock``。"""
        if new_state == self._state:
            return
        self._state = new_state
        if new_state == HALF_OPEN:
            self._half_open_calls = 0
        self._publish()

    def _publish(self) -> None:
        METRICS.set_gauge(
            "fuguoai_circuit_state",
            _STATE_GAUGE[self._state],
            labels={"name": self.name},
            help_text="熔断器状态 0=closed 1=open 2=half_open",
        )

    def _on_success(self) -> None:
        with self._lock:
            if self._state == HALF_OPEN:
                self._transition(CLOSED)
            self._fail_count = 0

    def _on_failure(self) -> None:
        with self._lock:
            self._fail_count += 1
            if self._state == HALF_OPEN:
                self._trip()  # 探测失败 → 重新开路并重新计时
            elif self._state == CLOSED and self._fail_count >= self.failure_threshold:
                self._trip()

    def _trip(self) -> None:
        """转入 OPEN 并记一次跳闸指标；调用方须持 ``_lock``。"""
        self._state = OPEN
        self._opened_at = time.monotonic()
        self._publish()
        METRICS.inc(
            "fuguoai_circuit_trips_total",
            labels={"name": self.name},
            help_text="熔断器跳闸（转入开路）总次数",
        )

    # ---------------- 放行判定 ----------------
    def allow(self) -> bool:
        """是否放行本次调用（公开版，供 async 上下文安全调用）。"""
        with self._lock:
            self._maybe_recover()
            if self._state == OPEN:
                return False
            if self._state == HALF_OPEN:
                if self._half_open_calls >= self.half_open_max_calls:
                    return False
                self._half_open_calls += 1
            return True

    def record_success(self) -> None:
        """记录一次成功调用（公开版）。"""
        self._on_success()

    def record_failure(self) -> None:
        """记录一次失败调用（公开版）。"""
        self._on_failure()

    def reset(self) -> None:
        """手动复位到 CLOSED（运维/测试用）。"""
        with self._lock:
            self._fail_count = 0
            self._half_open_calls = 0
            self._transition(CLOSED)

    # ---------------- 同步调用包装 ----------------
    def call(self, func, *args, **kwargs):
        """同步执行 func；开路时抛 ``CircuitOpenError``，成功/失败均驱动状态机。"""
        if not self.allow():
            raise CircuitOpenError(self.name)
        try:
            result = func(*args, **kwargs)
        except Exception:
            self._on_failure()
            raise
        else:
            self._on_success()
            return result


def get_breaker(name: str, **kwargs) -> CircuitBreaker:
    """获取（或创建）具名熔断器，全局唯一。"""
    with _REGISTRY_LOCK:
        cb = _REGISTRY.get(name)
        if cb is None:
            cb = CircuitBreaker(name, **kwargs)
            _REGISTRY[name] = cb
        return cb


def all_breakers() -> "list[CircuitBreaker]":
    """返回全部已注册熔断器（供 /healthz 汇总）。"""
    with _REGISTRY_LOCK:
        return list(_REGISTRY.values())


def retry_with_backoff(
    func,
    *args,
    retries: int = 2,
    base_delay: float = 0.3,
    max_delay: float = 2.0,
    exceptions: tuple = (Exception,),
    on_retry=None,
    jitter: bool = True,
    **kwargs,
):
    """指数退避重试同步函数。

    ``retries=N`` 表示最多额外重试 N 次（共 N+1 次尝试）；全部失败才向上抛。
    默认开启「去相关抖动」：sleep 在 [base_delay, delay*3] 间均匀取值，避免多个调用者
    在相同时钟节奏上重试而引发共振风暴；``jitter=False`` 退化为裸指数退避（兜底兼容）。
    """
    attempt = 0
    delay = base_delay
    while True:
        try:
            return func(*args, **kwargs)
        except exceptions as e:
            attempt += 1
            if attempt > retries:
                raise
            if on_retry:
                try:
                    on_retry(attempt, e)
                except Exception:
                    pass
            if jitter:
                sleep_for = min(max_delay, random.uniform(base_delay, delay * 3))
            else:
                sleep_for = min(delay, max_delay)
            time.sleep(sleep_for)
            delay *= 2
