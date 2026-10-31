# -*- coding: utf-8 -*-
"""核心指标预声明（离线）。

验证 declare_core_metrics：persona/eval 计数器启动即注册到 /metrics（HELP/TYPE 就绪、
首用前即出现），解除 1.0.0 进位受阻。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fuguoai_core.utils.metrics import METRICS, declare_core_metrics

EXPECTED_COUNTERS = (
    "fuguoai_persona_injections_total",
    "fuguoai_persona_api_total",
    "fuguoai_eval_total",
    "fuguoai_eval_low_quality_total",
    "fuguoai_eval_safety_alerts_total",
    "fuguoai_eval_errors_total",
)
EXPECTED_GAUGES = ("fuguoai_eval_last_overall",)


def test_declare_core_metrics_registers():
    # 全新单例，确保声明生效
    declare_core_metrics()
    counters = METRICS.snapshot_counters()
    gauges = METRICS.snapshot_gauges()
    for name in EXPECTED_COUNTERS:
        assert name in counters, f"计数器未注册: {name}"
        assert counters[name] == {}, f"预声明应为空: {name}"
    for name in EXPECTED_GAUGES:
        assert name in gauges, f"gauge 未注册: {name}"
    # 幂等：重复声明不报错
    declare_core_metrics()


if __name__ == "__main__":
    test_declare_core_metrics_registers()
    print("test_metrics_declare: ALL PASS")
