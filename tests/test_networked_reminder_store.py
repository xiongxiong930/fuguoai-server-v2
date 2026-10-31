"""联网提醒存储离线测试（无网络/无容器依赖，纯逻辑 + JSON 持久化）。

覆盖：4 类调度 next_fire_at 计算 / 校验 / 配额 / mark_fired 重算 /
pending_due 重试门限 / 列表范围 / 启用禁用。
"""

import os
import sys
import tempfile
import time
from datetime import datetime

# 隔离数据目录，避免污染真数据
_TMP = tempfile.mkdtemp(prefix="nrtest_")
os.environ["FUGUOAI_DATA_DIR"] = _TMP

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import fuguoai_core.utils.networked_reminder_store as nrs_mod  # noqa: E402
from fuguoai_core.utils.networked_reminder_store import (  # noqa: E402
    NetworkedReminderStore,
    _compute_next,
    _validate_schedule,
    _once_relative_seconds,
    _normalize_once,
    ONCE_FIRE_GRACE_SEC,
    MAX_PER_DEVICE,
)

NOW = 1_700_000_000  # 固定基准时间

# 统一把 store 的内部时钟钉到 NOW：add()/set_enabled 等不传 now 的路径都走
# _now()，不打桩会用真实时钟(2026)，令所有 NOW±N 的 fixture 变成"过去"被拒收。
nrs_mod._now = lambda: NOW


def _ts(y, m, d, H=0, M=0, S=0):
    return int(datetime(y, m, d, H, M, S).timestamp())


def mkstore():
    return NetworkedReminderStore(path=os.path.join(tempfile.mkdtemp(), "nr.json"))


def test_compute_next_once():
    assert _compute_next({"type": "once", "fire_at": 2000}, 1000) == 2000
    assert _compute_next({"type": "once", "fire_at": 0}, 1000) is None
    assert _compute_next({"type": "once"}, 1000) is None


def test_compute_next_daily():
    # 2023-11-14 10:00 周二；daily 08:00 → 次日 08:00
    now = _ts(2023, 11, 14, 10, 0, 0)
    nxt = _compute_next({"type": "daily", "hour": 8, "minute": 0}, now)
    assert nxt == _ts(2023, 11, 15, 8, 0, 0)
    # 若当前已过今日 08:00，则明天
    now2 = _ts(2023, 11, 14, 7, 0, 0)
    nxt2 = _compute_next({"type": "daily", "hour": 8, "minute": 0}, now2)
    assert nxt2 == _ts(2023, 11, 14, 8, 0, 0)


def test_compute_next_weekly():
    # 2023-11-14 周二(weekday=1)；repeat=[0,2,4]=周一/三/五 → 下个周三 11-15 08:00
    now = _ts(2023, 11, 14, 10, 0, 0)
    nxt = _compute_next({"type": "weekly", "hour": 8, "minute": 0, "repeat": [0, 2, 4]}, now)
    assert nxt == _ts(2023, 11, 15, 8, 0, 0)
    # repeat 用名字
    nxt2 = _compute_next({"type": "weekly", "hour": 8, "minute": 0, "repeat": ["mon", "wed", "fri"]}, now)
    assert nxt2 == _ts(2023, 11, 15, 8, 0, 0)
    # 空 repeat → None
    assert _compute_next({"type": "weekly", "hour": 8, "minute": 0, "repeat": []}, now) is None


def test_compute_next_interval():
    assert _compute_next({"type": "interval", "every_seconds": 1800}, 1000) == 2800
    # 已触发过 → 从上次触发 + every
    assert _compute_next({"type": "interval", "every_seconds": 1800}, 1000, last_fired_at=900) == 2700
    # every < MIN_INTERVAL_SEC(900) → None
    assert _compute_next({"type": "interval", "every_seconds": 30}, 1000) is None
    assert _compute_next({"type": "interval", "every_seconds": 899}, 1000) is None
    assert _compute_next({"type": "interval", "every_seconds": 900}, 1000) == 1900


def test_validate_schedule():
    assert _validate_schedule({"type": "once", "fire_at": NOW + 100})[0]
    assert _validate_schedule({"type": "once", "in_seconds": 60})[0]      # 相对时间合法
    assert _validate_schedule({"type": "once", "in_minutes": 30})[0]
    assert not _validate_schedule({"type": "once"})[0]               # 缺 fire_at
    assert not _validate_schedule({"type": "once", "fire_at": NOW - 3600})[0]  # 过去拒收
    assert _validate_schedule({"type": "once", "fire_at": NOW - 30})[0]  # 宽限内受理
    assert not _validate_schedule({"type": "bogus"})[0]              # 未知类型
    assert _validate_schedule({"type": "daily", "hour": 8, "minute": 0})[0]
    assert not _validate_schedule({"type": "daily", "hour": 25})[0]  # hour 越界
    assert _validate_schedule({"type": "weekly", "hour": 8, "minute": 0, "repeat": [1]})[0]
    assert not _validate_schedule({"type": "weekly", "hour": 8, "minute": 0})[0]  # 缺 repeat
    # interval 下限 900s（2026-10-17 8818 180s 连响复盘后上调）
    assert _validate_schedule({"type": "interval", "every_seconds": 900})[0]
    assert not _validate_schedule({"type": "interval", "every_seconds": 899})[0]
    assert not _validate_schedule({"type": "interval", "every_seconds": 60})[0]
    assert not _validate_schedule({"type": "interval"})[0]           # 缺 every_seconds
    assert not _validate_schedule({"type": "interval", "every_seconds": "900"})[0]  # 非整数


def test_add_and_get():
    s = mkstore()
    rid, e = s.add("devA", "u1", "喝水", {"type": "once", "fire_at": NOW + 100})
    assert rid and e is None
    item = s.get("devA", rid)
    assert item["text"] == "喝水"
    assert item["next_fire_at"] == NOW + 100
    assert item["status"] == "pending"


def test_once_relative_normalized_to_fire_at():
    # in_seconds/in_minutes → 绝对 fire_at（LLM 唯一算不错的时间表达）
    s = mkstore()
    rid, e = s.add("devA", "u1", "喝水", {"type": "once", "in_minutes": 30})
    assert rid and e is None
    item = s.get("devA", rid)
    assert item["schedule"] == {"type": "once", "fire_at": NOW + 1800}, item["schedule"]
    assert item["next_fire_at"] == NOW + 1800
    # 调用方原始 dict 不被改写
    raw = {"type": "once", "in_seconds": 60}
    s.add("devA", "u1", "x", raw)
    assert raw == {"type": "once", "in_seconds": 60}
    # fire_at 与相对字段同时给出 → 相对字段胜出
    rid2, _ = s.add("devA", "u1", "y", {"type": "once", "fire_at": NOW + 9999, "in_seconds": 60})
    assert s.get("devA", rid2)["schedule"]["fire_at"] == NOW + 60


def test_add_once_past_fire_at_rejected():
    # 2026-10-18 生产实证：LLM 把 fire_at 算成一年前 → 创建后立即触发。
    # 现在过去时间一律拒收，错误信息带当前北京时间供调用方自我纠正。
    s = mkstore()
    rid, e = s.add("devA", "u1", "喝水", {"type": "once", "fire_at": NOW - 3600})
    assert rid is None and e
    assert "过去" in e and "北京时间" in e, e
    # 宽限内（<=60s）仍受理
    rid, e = s.add("devA", "u1", "喝水", {"type": "once", "fire_at": NOW - 30})
    assert rid and e is None


def test_once_relative_seconds_helper():
    assert _once_relative_seconds({"in_seconds": 30}) == 30
    assert _once_relative_seconds({"in_minutes": 2}) == 120
    assert _once_relative_seconds({"in_seconds": 10, "in_minutes": 1}) == 70
    assert _once_relative_seconds({"in_seconds": -5}) == 0     # 非正数忽略
    assert _once_relative_seconds({"in_seconds": "30"}) == 0   # 非数字忽略
    assert _once_relative_seconds({}) == 0
    assert _once_relative_seconds(None) == 0
    sch = {"type": "once", "in_minutes": 5}
    assert _normalize_once(sch, NOW) is True
    assert sch == {"type": "once", "fire_at": NOW + 300}
    assert _normalize_once({"type": "daily", "in_minutes": 5}, NOW) is False  # 非 once 不动
    assert _normalize_once({"type": "once"}, NOW) is False                    # 无相对字段不动


def test_add_validation_and_quota():
    s = mkstore()
    rid, e = s.add("devA", "u1", "", {"type": "once", "fire_at": NOW + 1})
    assert rid is None and e  # 空 text
    rid, e = s.add("devA", "u1", "x" * 201, {"type": "once", "fire_at": NOW + 1})
    assert rid is None and "字" in e  # 超长
    # interval 低于 900s 下限拒绝入库（8818 nr538620498bb0 180s 复盘）
    rid, e = s.add("devA", "u1", "活动", {"type": "interval", "every_seconds": 899})
    assert rid is None and "900" in e
    rid, e = s.add("devA", "u1", "活动", {"type": "interval", "every_seconds": 900})
    assert rid and e is None
    for i in range(MAX_PER_DEVICE):
        r, _ = s.add("devQ", "u1", f"r{i}", {"type": "interval", "every_seconds": 3600})
        assert r
    r, e = s.add("devQ", "u1", "overflow", {"type": "interval", "every_seconds": 3600})
    assert r is None and "最多" in e  # 配额


def test_list_scoping_and_sort():
    s = mkstore()
    r1, _ = s.add("devA", "u1", "a", {"type": "daily", "hour": 8, "minute": 0})
    r2, _ = s.add("devA", "u1", "b", {"type": "once", "fire_at": NOW + 10})
    s.add("devB", "u2", "c", {"type": "once", "fire_at": NOW + 5})
    items = s.list("devA")
    assert len(items) == 2
    assert items[0]["rid"] == r2  # once(near) 排在 daily(明天) 前
    # 不跨设备
    assert len(s.list("devB")) == 1


def test_cancel():
    s = mkstore()
    rid, _ = s.add("devA", "u1", "a", {"type": "once", "fire_at": NOW + 10})
    assert s.cancel("devA", rid) is True
    assert s.get("devA", rid) is None
    assert s.cancel("devA", "nope") is False


def test_disable_enable():
    s = mkstore()
    rid, _ = s.add("devA", "u1", "a", {"type": "daily", "hour": 8, "minute": 0})
    assert s.set_enabled("devA", rid, False) is True
    assert s.get("devA", rid)["status"] == "disabled"
    assert s.list("devA") == []  # 默认不含 disabled
    assert len(s.list("devA", include_disabled=True)) == 1
    # 重新启用 → 恢复 pending 并补算 next
    assert s.set_enabled("devA", rid, True) is True
    it = s.get("devA", rid)
    assert it["status"] == "pending"
    assert it["next_fire_at"] and it["next_fire_at"] > NOW


def test_mark_fired_once():
    s = mkstore()
    rid, _ = s.add("devA", "u1", "a", {"type": "once", "fire_at": NOW - 5})
    assert s.mark_fired("devA", rid, NOW) is True
    it = s.get("devA", rid)
    assert it["status"] == "fired"
    assert it["next_fire_at"] is None
    assert it["fire_count"] == 1


def test_mark_fired_repeating_recompute():
    s = mkstore()
    rid, _ = s.add("devA", "u1", "a", {"type": "interval", "every_seconds": 1800})
    assert s.mark_fired("devA", rid, NOW) is True
    it = s.get("devA", rid)
    assert it["status"] == "pending"
    assert it["next_fire_at"] == NOW + 1800
    assert it["fire_count"] == 1
    # daily 重算到明天
    r2, _ = s.add("devA", "u1", "b", {"type": "daily", "hour": 8, "minute": 0})
    s.mark_fired("devA", r2, _ts(2023, 11, 14, 10, 0, 0))
    assert s.get("devA", r2)["next_fire_at"] == _ts(2023, 11, 15, 8, 0, 0)


def test_pending_due_gating():
    s = mkstore()
    # 到点 + 无拦截 → 命中
    rid, _ = s.add("devA", "u1", "a", {"type": "once", "fire_at": NOW - 5})
    due = s.pending_due(NOW)
    assert any(it["rid"] == rid for it in due)
    # 设备忙门限内 → 排除
    s.set_attempt("devA", rid, NOW, "device")
    assert not any(it["rid"] == rid for it in s.pending_due(NOW))
    # 门限过后(模拟 now 跳到 20s 后) → 重新命中
    assert any(it["rid"] == rid for it in s.pending_due(NOW + 20))
    # TTS 冷却内 → 排除
    s.set_attempt("devA", rid, NOW + 20, "tts", "fail")
    assert not any(it["rid"] == rid for it in s.pending_due(NOW + 25))
    # 冷却过后(300s) → 命中
    assert any(it["rid"] == rid for it in s.pending_due(NOW + 400))


def test_pending_due_future_excluded():
    s = mkstore()
    rid, _ = s.add("devA", "u1", "a", {"type": "once", "fire_at": NOW + 100})
    assert not any(it["rid"] == rid for it in s.pending_due(NOW))


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
