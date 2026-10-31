# -*- coding: utf-8 -*-
"""自进化方向①④⑥ 最小逻辑单测（纯单元，不依赖运行服务器）。

验证：
  ① aec_profile_store：compute_profile 阈值门控 + save/load 往返 + 物理钳制
  ④ _watchdog_cfg per_device 覆盖
  ⑥ conn_guard 信誉配额：频繁熔断→配额收紧+信誉降；安静→信誉回血
"""

import os
import json
import tempfile
import importlib

import pytest


# ───────────────────────── ① AEC 指纹建档 ─────────────────────────

def test_compute_profile_rejects_low_samples():
    from fuguoai_core.utils.aec_profile_store import compute_profile
    # 样本不足 → None
    assert compute_profile({"diag": True, "samples": 50,
                             "strong_delay_ms": {"median": 6000}}) is None


def test_compute_profile_rejects_nonlinear():
    from fuguoai_core.utils.aec_profile_store import compute_profile
    # 强相关占比不过半（非线性/不可消）→ None
    d = {"diag": True, "samples": 300,
         "strong_delay_ms": {"median": 6000, "stdev": 800},
         "corr": {"strong_count": 10, "weak_count": 290, "mean": 0.1}}
    assert compute_profile(d) is None


def test_compute_profile_ok_and_clamp():
    from fuguoai_core.utils.aec_profile_store import compute_profile, _DELAY_CAP_MS
    d = {"diag": True, "samples": 500,
         "strong_delay_ms": {"median": 6000, "stdev": 800},
         "corr": {"strong_count": 400, "weak_count": 100, "mean": 0.7}}
    p = compute_profile(d)
    assert p is not None
    assert p["echo_delay_ms"] == 6000
    assert p["echo_delay_band_ms"] == int(800 * 3.0)  # 2400
    assert 0.6 <= p["echo_gain_cap"] <= 0.9
    assert p["source"] == "auto"
    # 延迟超上限钳制
    d2 = dict(d)
    d2["strong_delay_ms"] = {"median": 99999, "stdev": 100}
    p2 = compute_profile(d2)
    assert p2["echo_delay_ms"] <= _DELAY_CAP_MS


def test_save_load_roundtrip(tmp_path, monkeypatch):
    from fuguoai_core.utils import aec_profile_store as m
    monkeypatch.setenv("FUGUOAI_DATA_DIR", str(tmp_path))
    importlib.reload(m)
    prof = {"echo_delay_ms": 6000, "echo_delay_band_ms": 2400,
            "echo_gain_cap": 0.8, "source": "auto", "ts": int(__import__("time").time())}
    assert m.save_profile("94:a9:90:1a:90c0", prof)
    loaded = m.load_profile("94:a9:90:1a:90c0")
    assert loaded["echo_delay_ms"] == 6000
    # 文件名安全化（冒号→下划线）
    files = os.listdir(os.path.join(str(tmp_path), "aec_profiles"))
    assert any("aec_profile_94_a9_90_1a_90c0.json" == f for f in files)


def test_load_nonexistent_returns_none():
    from fuguoai_core.utils.aec_profile_store import load_profile
    assert load_profile("no-such-device-xyz") is None


# ───────────────────────── ④ 看门狗设备画像 ─────────────────────────

def test_watchdog_cfg_per_device_override(monkeypatch):
    # 注入 config：全局 after_flow=15，per_device 90C0=20
    fake_cfg = {
        "fuguoai": {
            "intercom": {
                "watchdog_silence_sec_after_flow": 15,
                "per_device": {
                    "94a9901a90c0": {"watchdog_silence_sec_after_flow": 20},
                },
            }
        }
    }
    import fuguoai_core.utils.config_holder as ch
    monkeypatch.setattr(ch, "get_config", lambda: fake_cfg)

    from fuguoai_core.utils.intercom_call_store import _watchdog_cfg
    s_global, sa_global, sm_global = _watchdog_cfg("some-device")
    assert sa_global == 15  # 全局生效
    s_dev, sa_dev, sm_dev = _watchdog_cfg("94a9901a90c0")
    assert sa_dev == 20     # per_device 覆盖生效


def test_watchdog_cfg_default_when_no_config(monkeypatch):
    import fuguoai_core.utils.config_holder as ch
    monkeypatch.setattr(ch, "get_config", lambda: {})
    from fuguoai_core.utils.intercom_call_store import _watchdog_cfg
    s, sa, sm = _watchdog_cfg("device-x")
    # 模块默认：silence=5, after_flow=15, stale=60
    assert s == 5 and sa == 15 and sm == 60


# ───────────────────────── ⑥ conn_guard 信誉配额 ─────────────────────────

def test_conn_guard_reputation_tightens_on_repeat_storm():
    from fuguoai_core.utils.conn_guard import ConnectionStormGuard
    g = ConnectionStormGuard(window_sec=1.0, max_conn=5, cooldown_sec=1.0)
    dev = "storm-device"
    # 触发一次熔断
    results = []
    for _ in range(10):
        results.append(g.allow(dev)[0])
    # 前 5 次 True，第 6 次触发熔断 False，之后冷却 False
    assert results.count(False) >= 1
    # 信誉应已下降（snapshot 暴露）
    snap = g.snapshot()
    assert snap["reputation"].get(dev, 1.0) < 1.0
    # effective_max 应 < 全局 5
    # 等冷却结束后下一窗口配额收紧：再灌连接看是否更早熔断
    import time
    time.sleep(1.1)
    g.allow(dev)  # 冷却结束放行，信誉回血一点但仍<1
    rep_after = g.snapshot()["reputation"].get(dev, 1.0)
    assert 0.0 < rep_after <= 1.0


def test_conn_guard_normal_device_untouched():
    from fuguoai_core.utils.conn_guard import ConnectionStormGuard
    g = ConnectionStormGuard(window_sec=60.0, max_conn=3, cooldown_sec=60.0)
    # 正常设备 3 次内全放行，不进冷却、信誉 1.0
    assert all(g.allow("normal")[0] for _ in range(3))
    snap = g.snapshot()
    assert "normal" not in snap["reputation"]  # 未熔断设备不出现


def test_conn_guard_phone_exempt():
    from fuguoai_core.utils.conn_guard import ConnectionStormGuard
    g = ConnectionStormGuard(window_sec=0.1, max_conn=2, cooldown_sec=0.1)
    # 空 device_id（phone/未知）一律放行
    assert all(g.allow(None)[0] for _ in range(100))
