#!/usr/bin/env python3
# 复果AI V3 弹性测试台（T11 熔断降级恢复）
# 在容器内运行：直接导入 circuit_breaker 模块，验证"单点故障 → 快速失败降级 → 自动恢复"闭环。
# 使用缩短的 recovery_timeout（模拟 30s 恢复窗口的按比例验证），保证测试确定、快速。
import sys
import time

sys.path.insert(0, "/opt/fuguoai-server")

from fuguoai_core.utils.circuit_breaker import CircuitBreaker, CircuitOpenError, retry_with_backoff
from fuguoai_core.utils.metrics import METRICS


def log(m):
    print(m, flush=True)


def test_circuit_recovery():
    """故障注入：连续失败开路 → 快速失败（降级点）→ 恢复窗口后半开探测成功 → 闭合恢复。"""
    cb = CircuitBreaker("t11", failure_threshold=3, recovery_timeout=1.0, half_open_max_calls=1)

    def boom():
        raise RuntimeError("dependency down")

    def ok():
        return "ok"

    # 1) 注入连续故障，触发开路
    for _ in range(3):
        try:
            cb.call(boom)
        except Exception:
            pass
    opened = cb.state == "open"

    # 2) 开路期间必须快速失败（不再打后端 → 降级生效）
    t0 = time.monotonic()
    fast_fail = False
    try:
        cb.call(ok)
    except CircuitOpenError:
        fast_fail = (time.monotonic() - t0) < 0.05  # 立即失败，未走远程
    except Exception:
        fast_fail = False

    # 3) 等待恢复窗口 → 半开
    time.sleep(1.1)
    half = cb.state == "half_open"

    # 4) 半开探测成功 → 闭合（恢复）
    probe = cb.call(ok)
    recovered = cb.state == "closed" and probe == "ok"

    ok_all = opened and fast_fail and half and recovered
    log(
        f"[T11 熔断恢复] 开路={opened} 快速失败={fast_fail} 半开={half} 恢复闭合={recovered} "
        f"-> {'PASS' if ok_all else 'FAIL'}"
    )
    return ok_all


def test_retry_backoff():
    """瞬时抖动：退避重试在阈值内自愈；超过阈值抛出。"""
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        return "recovered"

    r = retry_with_backoff(flaky, retries=3, base_delay=0.01)
    ok = r == "recovered" and calls["n"] == 3
    log(f"[T11b 退避重试] 结果={r} 尝试次数={calls['n']} -> {'PASS' if ok else 'FAIL'}")
    return ok


def test_metrics_wiring():
    """熔断跳闸计入指标，可被 /metrics 抓取。"""
    before = METRICS.render()
    cb = CircuitBreaker("t11c", failure_threshold=2, recovery_timeout=5.0)

    def boom():
        raise RuntimeError("x")

    for _ in range(2):
        try:
            cb.call(boom)
        except Exception:
            pass
    after = METRICS.render()
    ok = 'fuguoai_circuit_trips_total{name="t11c"}' in after and 'fuguoai_circuit_state{name="t11c"} 1' in after
    log(f"[T11c 指标联动] 跳闸计数&状态已写入指标 -> {'PASS' if ok else 'FAIL'}")
    return ok


def main():
    results = {
        "circuit_recovery": test_circuit_recovery(),
        "retry_backoff": test_retry_backoff(),
        "metrics_wiring": test_metrics_wiring(),
    }
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    log("")
    log("=" * 50)
    log(f"V3 弹性测试结果: {passed}/{total} 通过")
    for k, v in results.items():
        log(f"  {k}: {'PASS' if v else 'FAIL'}")
    log("=" * 50)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
