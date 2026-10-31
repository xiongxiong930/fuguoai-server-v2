"""离线测试：连接风暴熔断器（fuguoai_core/utils/conn_guard.py）。

这个熔断器会**拒绝真实设备的连接**，误杀代价高（设备连不上＝设备变砖的用户感知），
因此测试重点不是"能不能熔断"，而是**在什么情况下绝不能熔断**：
  - 正常频率重连不触发
  - 低频长时间重连（滑动窗口过期）不触发
  - 一台设备熔断不牵连其他设备
  - 冷却到期必须自愈放行，绝不永久封禁
  - 冷却期内的拒绝不能反过来延长冷却（否则风暴设备永远出不来）

时间用可控假时钟注入，不依赖 sleep，保证门禁快且不 flaky。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fuguoai_core.utils import conn_guard as cg
from fuguoai_core.utils.conn_guard import ConnectionStormGuard, get_conn_storm_guard


class FakeClock:
    """可控时钟：替换 conn_guard 模块内的 time.time。"""

    def __init__(self, t0=1000.0):
        self.t = t0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _patch_clock(clock):
    class _T:
        time = staticmethod(clock)

    cg.time = _T


def _restore_clock():
    import time as _real_time

    cg.time = _real_time


def test_normal_reconnect_never_blocked():
    """正常重连频率：窗口内次数不超阈值，必须全部放行。"""
    clock = FakeClock()
    _patch_clock(clock)
    g = ConnectionStormGuard(window_sec=60.0, max_conn=15, cooldown_sec=60.0)
    # 60s 内 15 次（阈值边界，含最后一次）——真实场景已经很极端了，仍须放行
    for i in range(15):
        ok, why = g.allow("devA")
        assert ok, f"第 {i + 1} 次正常重连被误杀: {why}"
        clock.advance(3.0)
    print("PASS test_normal_reconnect_never_blocked")


def test_trip_on_storm():
    """超过阈值的那一次即熔断并拒绝。"""
    clock = FakeClock()
    _patch_clock(clock)
    g = ConnectionStormGuard(window_sec=60.0, max_conn=5, cooldown_sec=30.0)
    for i in range(5):
        assert g.allow("devB")[0], f"第 {i + 1} 次不该被拒"
        clock.advance(1.0)
    ok, why = g.allow("devB")
    assert not ok, "第 6 次(超阈值)应触发熔断"
    assert why == "storm_tripped", why
    print("PASS test_trip_on_storm")


def test_cooldown_rejects_without_extending():
    """冷却期内一律拒绝；且拒绝不登记历史，不会把冷却无限延长。"""
    clock = FakeClock()
    _patch_clock(clock)
    g = ConnectionStormGuard(window_sec=60.0, max_conn=3, cooldown_sec=30.0)
    for _ in range(3):
        assert g.allow("devC")[0]
    assert not g.allow("devC")[0]  # 熔断，冷却至 t+30

    # 冷却期内狂敲 100 次
    for _ in range(100):
        clock.advance(0.1)
        ok, why = g.allow("devC")
        assert not ok
        assert why.startswith("storm_cooldown"), why

    # 关键：冷却截止时间没有被这 100 次拒绝往后推
    snap = g.snapshot()
    remain = snap["cooling"]["devC"]
    assert remain <= 20.1, f"冷却被拒绝请求延长了: remain={remain}"
    print("PASS test_cooldown_rejects_without_extending")


def test_self_heal_after_cooldown():
    """冷却到期必须自愈放行（核心不变量）；但自进化⑥：反复风暴设备信誉已降，
    自愈后配额收紧（eff_max<全局 max），不再能立刻用满原配额。"""
    clock = FakeClock()
    _patch_clock(clock)
    g = ConnectionStormGuard(window_sec=60.0, max_conn=3, cooldown_sec=30.0)
    for _ in range(3):
        g.allow("devD")
    assert not g.allow("devD")[0]

    clock.advance(31.0)  # 冷却结束
    ok, why = g.allow("devD")
    assert ok, f"冷却结束后必须放行(自愈): {why}"

    # 信誉回血一点但仍 <1.0（snapshot 可见），故 eff_max 收紧：
    # 冷却后还能再放行 (eff_max-1) 次，而 eff_max = max(2, 3*rep) < 3。
    rep = g.snapshot()["reputation"].get("devD", 1.0)
    assert rep < 1.0, "风暴设备冷却后信誉应已下降"
    eff_max = max(2, int(3 * rep))
    # 再放 (eff_max-1) 次应当成功，第 eff_max 次触发收紧后的熔断
    for i in range(eff_max - 1):
        assert g.allow("devD")[0], f"自愈后第 {i + 2} 次应放行(收紧配额内)"
    assert not g.allow("devD")[0], "信誉收紧后配额用尽应再次熔断"
    print("PASS test_self_heal_after_cooldown")


def test_sliding_window_expiry():
    """低频重连：老记录滑出窗口后不累计，长时间运行也不会被误杀。"""
    clock = FakeClock()
    _patch_clock(clock)
    g = ConnectionStormGuard(window_sec=60.0, max_conn=5, cooldown_sec=30.0)
    # 每 30s 连一次，连 50 次（跨 25 分钟）——窗口内始终只有 2~3 条
    for i in range(50):
        ok, why = g.allow("devE")
        assert ok, f"低频重连第 {i + 1} 次被误杀: {why}"
        clock.advance(30.0)
    print("PASS test_sliding_window_expiry")


def test_per_device_isolation():
    """一台设备风暴不得牵连其他设备。"""
    clock = FakeClock()
    _patch_clock(clock)
    g = ConnectionStormGuard(window_sec=60.0, max_conn=3, cooldown_sec=30.0)
    for _ in range(3):
        g.allow("bad")
    assert not g.allow("bad")[0]

    for i in range(3):
        ok, why = g.allow("good")
        assert ok, f"无辜设备被牵连: {why}"
    print("PASS test_per_device_isolation")


def test_empty_device_always_allowed():
    """device_id 为空（未知设备 / phone 端）一律放行，绝不误伤。"""
    clock = FakeClock()
    _patch_clock(clock)
    g = ConnectionStormGuard(window_sec=60.0, max_conn=2, cooldown_sec=30.0)
    for _ in range(200):
        assert g.allow(None)[0]
        assert g.allow("")[0]
    print("PASS test_empty_device_always_allowed")


def test_configure_and_invalid_values():
    """configure 可覆盖阈值；非法值必须被忽略而不是把阈值搞坏。"""
    _restore_clock()
    g = ConnectionStormGuard()
    g.configure({"window_sec": 10, "max_conn": 3, "cooldown_sec": 5})
    snap = g.snapshot()
    assert snap["window_sec"] == 10.0 and snap["max_conn"] == 3
    assert snap["cooldown_sec"] == 5.0

    g.configure({"max_conn": "not-a-number"})
    snap2 = g.snapshot()
    assert snap2["max_conn"] == 3, f"非法值污染了阈值: {snap2}"

    g.configure(None)  # 不应抛
    assert g.snapshot()["max_conn"] == 3
    print("PASS test_configure_and_invalid_values")


def test_reset_and_singleton():
    """reset 清状态；单例可重复获取同一实例。"""
    clock = FakeClock()
    _patch_clock(clock)
    g = ConnectionStormGuard(window_sec=60.0, max_conn=2, cooldown_sec=30.0)
    g.allow("devF")
    g.allow("devF")
    assert not g.allow("devF")[0]
    g.reset("devF")
    assert g.allow("devF")[0], "reset 后应立即放行"

    g.reset()
    assert g.snapshot()["tracked"] == {}

    assert get_conn_storm_guard() is get_conn_storm_guard()
    print("PASS test_reset_and_singleton")


def test_metrics_recorded():
    """熔断与拒绝都要有 metrics，便于线上判断是否误杀。"""
    from fuguoai_core.utils.metrics import METRICS

    clock = FakeClock()
    _patch_clock(clock)
    g = ConnectionStormGuard(window_sec=60.0, max_conn=2, cooldown_sec=30.0)
    dev = "metricdev"
    g.allow(dev)
    g.allow(dev)
    g.allow(dev)  # trip
    g.allow(dev)  # rejected in cooldown

    counters = METRICS.snapshot_counters()
    tripped = counters.get("fuguoai_conn_storm_tripped_total", {})
    rejected = counters.get("fuguoai_conn_storm_rejected_total", {})
    key = (("device_id", dev),)
    assert tripped.get(key, 0) >= 1, f"熔断未计数: {tripped}"
    assert rejected.get(key, 0) >= 2, f"拒绝未计数: {rejected}"
    print("PASS test_metrics_recorded")


if __name__ == "__main__":
    try:
        test_normal_reconnect_never_blocked()
        test_trip_on_storm()
        test_cooldown_rejects_without_extending()
        test_self_heal_after_cooldown()
        test_sliding_window_expiry()
        test_per_device_isolation()
        test_empty_device_always_allowed()
        test_configure_and_invalid_values()
        test_reset_and_singleton()
        test_metrics_recorded()
    finally:
        _restore_clock()
    print("ALL PASS")
