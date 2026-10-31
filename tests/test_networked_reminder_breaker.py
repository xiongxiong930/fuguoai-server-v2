"""联网提醒熔断测试（BurstGuard + fire_one auto 计数）。

背景：2026-10-17 复盘 8818「站立活动一下」interval 180s 无人看管连响
11.8 天 / 1388 次。熔断语义：仅 worker 自动触发计数，滑动窗口 1h 内超
6 次 → 自动 disabled + ERROR；手动「立即播报」不计数不熔断。
interval 900s 正常节奏 = 4 次/h，留 50% 余量，不应误伤。
"""

import asyncio
import os
import sys
import tempfile
import types

# intercom_registry 模块级 import opuslib_next（仅 relay 转码路径函数体内使用，
# 与本测试的熔断逻辑零关系）。本地无 libopus，注入 stub 打通 import 链；
# 159 容器内为真实包，部署行为不受影响。
if "opuslib_next" not in sys.modules:
    sys.modules["opuslib_next"] = types.ModuleType("opuslib_next")

_TMP = tempfile.mkdtemp(prefix="nrbreak_")
os.environ["FUGUOAI_DATA_DIR"] = _TMP

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from fuguoai_core.utils.networked_reminder_store import (  # noqa: E402
    NetworkedReminderStore,
    get_networked_reminder_store,
)
from fuguoai_core.utils import networked_reminder_scheduler as sch  # noqa: E402

NOW = 1_700_000_000


def _mkstore():
    return NetworkedReminderStore(path=os.path.join(tempfile.mkdtemp(), "nr.json"))


# ---------- BurstGuard 纯逻辑 ----------

def test_burst_guard_under_limit():
    g = sch.BurstGuard(window=3600, limit=6)
    for i in range(6):
        assert g.record("r1", NOW + i) is False   # 6 次内不熔断


def test_burst_guard_over_limit():
    g = sch.BurstGuard(window=3600, limit=6)
    for i in range(6):
        g.record("r1", NOW + i)
    assert g.record("r1", NOW + 7) is True        # 第 7 次熔断


def test_burst_guard_window_slides():
    g = sch.BurstGuard(window=3600, limit=6)
    # 先记一次很久以前的（应在后续记录时被滑出窗口）
    g.record("r1", NOW - 7200)
    # 窗口内 6 次：旧的那次已被清出，不熔断
    for i in range(6):
        assert g.record("r1", NOW + i) is False
    # 第 7 次（仍在窗口内）→ 熔断
    assert g.record("r1", NOW + 7) is True


def test_burst_guard_per_rid_isolated():
    g = sch.BurstGuard(window=3600, limit=6)
    for i in range(7):
        g.record("r1", NOW + i)
    assert g.record("r2", NOW) is False           # 熔断不跨 rid 传染


def test_burst_guard_reset():
    g = sch.BurstGuard(window=3600, limit=6)
    for i in range(7):
        g.record("r1", NOW + i)
    g.reset("r1")
    assert g.record("r1", NOW + 100) is False     # reset 后重新计数


# ---------- fire_one 集成（monkeypatch 设备/推流） ----------

def _patch_scheduler(store, ok=True):
    """替换 _device_playable / speak_text_to_device / store 单例，返回原引用。"""
    orig_store_fn = sch.get_networked_reminder_store
    sch.get_networked_reminder_store = lambda: store
    sch._device_playable = lambda did: True

    async def _fake_speak(device_id, text, label=None, voice=None):
        return ok

    orig_speak = sch.speak_text_to_device
    sch.speak_text_to_device = _fake_speak
    return (orig_store_fn, orig_speak)


def _restore(saved):
    orig_store_fn, orig_speak = saved
    sch.get_networked_reminder_store = orig_store_fn
    sch.speak_text_to_device = orig_speak


def test_fire_one_auto_breaker_disables():
    store = _mkstore()
    rid, _ = store.add("devB", "u1", "活动", {"type": "interval", "every_seconds": 900})
    sched = sch.NetworkedReminderScheduler()
    saved = _patch_scheduler(store, ok=True)
    try:
        # 前 6 次：正常 fire，status 保持 pending
        for i in range(6):
            ok = asyncio.run(sched.fire_one("devB", rid, "活动", auto=True))
            assert ok is True
            assert store.get("devB", rid)["status"] == "pending"
        # 第 7 次：触发熔断 → disabled
        ok = asyncio.run(sched.fire_one("devB", rid, "活动", auto=True))
        assert ok is True
        it = store.get("devB", rid)
        assert it["status"] == "disabled"
        assert it["fire_count"] == 7
    finally:
        _restore(saved)


def test_fire_one_manual_not_counted():
    store = _mkstore()
    rid, _ = store.add("devC", "u1", "喝水", {"type": "daily", "hour": 8, "minute": 0})
    sched = sch.NetworkedReminderScheduler()
    saved = _patch_scheduler(store, ok=True)
    try:
        # 手动连点 20 次也不熔断（用户主动操作）
        for i in range(20):
            ok = asyncio.run(sched.fire_one("devC", rid, "喝水"))
            assert ok is True
        assert store.get("devC", rid)["status"] == "pending"
    finally:
        _restore(saved)


def test_fire_one_auto_failure_not_counted():
    store = _mkstore()
    rid, _ = store.add("devD", "u1", "活动", {"type": "interval", "every_seconds": 900})
    sched = sch.NetworkedReminderScheduler()
    saved = _patch_scheduler(store, ok=False)
    try:
        # 连续失败 10 次：不计数不熔断（失败走 set_attempt 重试门限）
        for i in range(10):
            ok = asyncio.run(sched.fire_one("devD", rid, "活动", auto=True))
            assert ok is False
        assert store.get("devD", rid)["status"] == "pending"
        assert store.get("devD", rid)["fire_count"] == 0
    finally:
        _restore(saved)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
