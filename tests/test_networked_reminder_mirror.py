"""B 方案：云端提醒 → 设备本地表同步单测（_mirror_to_device_schedule）。

背景：2026-10-17 语音设提醒根因——networked_reminder_create 只写云端系统1
（到点推流），没接设备本地表（系统2 屏幕可见 + 设备本地触发）。本测试验证
daily/weekly/once 被正确映射成 cloud task 并经 schedule_push 下行；interval
仅留云端、不下设备；字段缺失/异常路径不阻断云端提醒创建。

2026-10-18 修正两处契约错位（固件 device_control.cc 回源）：
- once 曾被显式排除（旧注释"SetTimerTask 无 one-shot 语义"判断错误）→ 现按
  repeat=0 + date_y/date_m/date_d（北京墙钟）镜像，fire_at 已过去的跳过；
- weekly 位图旧按 bit0=周一 塞，与设备 bit0=周日 相反 → 现按 (d+1)%7 转换。

2026-10-18 新增 rid 闭环：create 把 rid 写进镜像条目；cancel 按 rid 精确删除
（同内容多条提醒不误删），内容匹配仅作 legacy 无 rid 存量的兜底。

复刻 breaker 测试手法：stub opuslib_next（本地无 libopus，util.py 模块级 import），
隔离 FUGUOAI_DATA_DIR，REPO 入 path。
"""

import asyncio
import os
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta, timezone

if "opuslib_next" not in sys.modules:
    sys.modules["opuslib_next"] = types.ModuleType("opuslib_next")

_TMP = tempfile.mkdtemp(prefix="nrmirror_")
os.environ["FUGUOAI_DATA_DIR"] = _TMP

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import fuguoai_core.utils.schedule_mirror_store as sms_mod  # noqa: E402
import fuguoai_core.utils.schedule_downlink as sdl_mod  # noqa: E402
import fuguoai_plugins.functions.networked_reminder as nr  # noqa: E402

_BJ = timezone(timedelta(hours=8))


def _install_fakes():
    """替换 lazily-imported 模块属性，捕获 create/push/publish/delete 行为。"""
    state = {"task": None, "pushed": None, "published": None, "deleted": [], "deleted_by_rid": []}

    class _FakeStore:
        def __init__(self):
            self.tasks = []

        def create_cloud_task(self, device_id, fields):
            # 2026-10-18 起云端 id 从 INT_MAX 递减分配（防 cJSON valueint 钳位）
            tid = 0x7FFFFFFF - len(self.tasks)
            task = {"id": tid, "source": "cloud", "modified_utc": "now", **fields}
            self.tasks.append(task)
            state["task"] = task
            return task

        def get_cloud_tasks(self, device_id):
            return list(self.tasks)

        def delete_cloud_task(self, device_id, task_id):
            before = len(self.tasks)
            self.tasks = [t for t in self.tasks if t.get("id") != task_id]
            state["deleted"].append(task_id)
            return len(self.tasks) != before

        def delete_cloud_task_by_rid(self, device_id, rid):
            for t in self.tasks:
                if t.get("rid") == rid:
                    self.tasks = [x for x in self.tasks if x.get("rid") != rid]
                    state["deleted_by_rid"].append(rid)
                    return t["id"]
            return None

    store = _FakeStore()
    sms_mod.get_schedule_mirror_store = lambda: store

    async def _fake_push(device_id, tasks=None, deletes=None):
        state["pushed"] = (device_id, tasks, deletes)
        return True

    sdl_mod.push_schedule_to_device = _fake_push

    def _fake_publish(device_id, action, task=None, deleted_id=None):
        state["published"] = (device_id, action, task)

    sdl_mod.publish_cloud_change = _fake_publish
    return state


def _clear_fakes():
    # 还原为不可调用，确保用例间隔离（函数内 from import 读即时属性）
    sms_mod.get_schedule_mirror_store = lambda: (_ for _ in ()).throw(RuntimeError("no store"))
    sdl_mod.push_schedule_to_device = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no push"))
    sdl_mod.publish_cloud_change = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no publish"))


# ---------- 正常映射 ----------

def test_mirror_daily_maps_to_repeat_127():
    st = _install_fakes()
    try:
        out = asyncio.run(nr._mirror_to_device_schedule("devX", "吃药", {"type": "daily", "hour": 9, "minute": 0}))
        assert out is None  # daily 走下行，函数本身不返回值
        t = st["task"]
        assert t["hour"] == 9 and t["minute"] == 0
        assert t["repeat"] == 127, f"daily 应 repeat=127(每天)，实际 {t['repeat']}"
        assert t["message"] == "吃药"
        assert st["pushed"][1] == [t], "应经 schedule_push 下发该 task"
        assert st["published"][1] == "create"
    finally:
        _clear_fakes()


def test_mirror_weekly_bitmask_device_convention():
    st = _install_fakes()
    try:
        # 周一(0)/周三(2)/周五(4)（LLM 约定 0=周一..6=周日）→ 设备位图 bit0=周日：
        # 周一→bit1、周三→bit3、周五→bit5 = 2+8+32 = 42
        out = asyncio.run(nr._mirror_to_device_schedule(
            "devY", "开会", {"type": "weekly", "hour": 14, "minute": 30, "repeat": [0, 2, 4]}))
        assert out is None
        t = st["task"]
        assert t["repeat"] == 42, f"weekly 位图应为 42(设备约定 bit0=周日)，实际 {t['repeat']}"
        assert t["message"] == "开会"
        assert st["pushed"][1] == [t]
    finally:
        _clear_fakes()


def test_mirror_weekly_sunday_maps_to_bit0():
    st = _install_fakes()
    try:
        # 周日(6) → bit0 = 1
        out = asyncio.run(nr._mirror_to_device_schedule(
            "devS", "休息", {"type": "weekly", "hour": 10, "minute": 0, "repeat": [6]}))
        assert out is None
        assert st["task"]["repeat"] == 1
    finally:
        _clear_fakes()


def test_mirror_weekly_everyday_is_127():
    st = _install_fakes()
    try:
        out = asyncio.run(nr._mirror_to_device_schedule(
            "devZ", "打卡", {"type": "weekly", "hour": 8, "minute": 0, "repeat": [0, 1, 2, 3, 4, 5, 6]}))
        assert out is None
        assert st["task"]["repeat"] == 127
    finally:
        _clear_fakes()


def test_mirror_once_future_maps_to_repeat0_with_date():
    st = _install_fakes()
    try:
        fa = int(time.time()) + 3600  # 一小时后
        bj = datetime.fromtimestamp(fa, tz=_BJ)
        out = asyncio.run(nr._mirror_to_device_schedule(
            "devO", "喝水", {"type": "once", "fire_at": fa}))
        assert out is None
        t = st["task"]
        assert t["repeat"] == 0, f"once 应 repeat=0(单次)，实际 {t['repeat']}"
        assert t["hour"] == bj.hour and t["minute"] == bj.minute
        assert (t["date_y"], t["date_m"], t["date_d"]) == (bj.year, bj.month, bj.day), \
            f"once 应带北京墙钟日期，实际 {(t.get('date_y'), t.get('date_m'), t.get('date_d'))}"
        assert t["message"] == "喝水"
        assert st["pushed"][1] == [t], "once 也应经 schedule_push 下行"
    finally:
        _clear_fakes()


# ---------- 不下设备（仅云端推流兜底） ----------

def test_mirror_once_past_skipped():
    st = _install_fakes()
    try:
        # 已过去（含 LLM 算错年份）：设备必隐藏（目标日期<今天），跳过镜像
        out = asyncio.run(nr._mirror_to_device_schedule("dev1", "明天8点起床", {"type": "once", "fire_at": 123456}))
        assert out is None
        assert st["task"] is None, "过去的 once 不应写设备本地表"
        assert st["pushed"] is None
    finally:
        _clear_fakes()


def test_create_once_relative_mirrors_with_date():
    # 端到端：LLM 用 in_minutes 相对时间 → store 归一成 fire_at → 镜像带 repeat=0+北京日期
    # （防回归：create 曾把原始 schedule 传给 mirror，相对时间没有 fire_at 会静默跳过）
    st = _install_fakes()
    try:
        import types as _t
        conn = _t.SimpleNamespace(device_id="devR")
        resp = asyncio.run(nr.networked_reminder_create(conn, "喝水", {"type": "once", "in_minutes": 30}))
        assert resp.action == nr.Action.REQLLM
        t = st["task"]
        assert t is not None, "相对时间 once 也应镜像到设备日程"
        assert t["repeat"] == 0
        bj = datetime.fromtimestamp(int(time.time()) + 1800, tz=_BJ)
        assert t["hour"] == bj.hour and t["minute"] == bj.minute
        assert (t["date_y"], t["date_m"], t["date_d"]) == (bj.year, bj.month, bj.day)
        assert t["message"] == "喝水"
    finally:
        _clear_fakes()


def test_create_validation_failure_returns_reqllm():
    # 创建失败走 REQLLM（result 回喂 LLM 可自我纠正），不再走 ERROR（被丢弃）
    st = _install_fakes()
    try:
        import types as _t
        conn = _t.SimpleNamespace(device_id="devV")
        resp = asyncio.run(nr.networked_reminder_create(
            conn, "喝水", {"type": "once", "fire_at": 123456}))  # 1970 的过去时间
        assert resp.action == nr.Action.REQLLM
        assert "创建提醒失败" in resp.result and "北京时间" in resp.result
        assert st["task"] is None  # 未创建 → 不镜像
    finally:
        _clear_fakes()


def test_mirror_interval_skipped():
    st = _install_fakes()
    try:
        out = asyncio.run(nr._mirror_to_device_schedule("dev2", "每15分钟喝水", {"type": "interval", "every_seconds": 900}))
        assert out is None
        assert st["task"] is None
        assert st["pushed"] is None
    finally:
        _clear_fakes()


def test_mirror_unknown_type_skipped():
    st = _install_fakes()
    try:
        out = asyncio.run(nr._mirror_to_device_schedule("dev3", "x", {"type": "bogus"}))
        assert out is None
        assert st["task"] is None
    finally:
        _clear_fakes()


# ---------- 取消路径：删除已镜像的设备任务 ----------

def test_cancel_deletes_mirrored_task():
    st = _install_fakes()
    try:
        # 先镜像一条 daily，再走取消工具 → 应按内容匹配删除并下行 deletes
        asyncio.run(nr._mirror_to_device_schedule("devC", "吃药", {"type": "daily", "hour": 9, "minute": 0}))
        assert st["task"] is not None
        import types as _t
        conn = _t.SimpleNamespace(device_id="devC")
        # 造一条同内容云端提醒（store 用隔离数据目录）
        store = nr.get_networked_reminder_store()
        rid, err = store.add("devC", None, "吃药", {"type": "daily", "hour": 9, "minute": 0})
        assert rid, err
        resp = asyncio.run(nr.networked_reminder_cancel(conn, rid))
        assert resp.action == nr.Action.REQLLM
        assert st["deleted"] == [st["task"]["id"]], f"应删除已镜像 task，实际 {st['deleted']}"
        assert st["pushed"][2] == [st["task"]["id"]], "删除应经 schedule_push deletes 下行"
        assert st["published"][1] == "delete"
    finally:
        _clear_fakes()


def test_cancel_without_mirror_is_noop():
    st = _install_fakes()
    try:
        import types as _t
        conn = _t.SimpleNamespace(device_id="devD")
        store = nr.get_networked_reminder_store()
        # interval 从不镜像 → 取消时无匹配、不误删
        rid, err = store.add("devD", None, "活动", {"type": "interval", "every_seconds": 900})
        assert rid, err
        resp = asyncio.run(nr.networked_reminder_cancel(conn, rid))
        assert resp.action == nr.Action.REQLLM
        assert st["deleted"] == []
        assert st["pushed"] is None
    finally:
        _clear_fakes()


# ---------- rid 闭环：create 写 rid / cancel 按 rid 精确删 ----------

def test_create_writes_rid_into_mirror():
    # create 工具把 rid 一并写入镜像条目（once 触发后 scheduler 按 rid 清理）
    st = _install_fakes()
    try:
        import types as _t
        conn = _t.SimpleNamespace(device_id="devRid")
        resp = asyncio.run(nr.networked_reminder_create(conn, "喝水", {"type": "daily", "hour": 9, "minute": 0}))
        assert resp.action == nr.Action.REQLLM
        t = st["task"]
        assert t is not None and t.get("rid"), "create 应把 rid 写入镜像条目"
        store = nr.get_networked_reminder_store()
        items = store.list("devRid")
        assert items and items[0]["rid"] == t["rid"], "镜像 rid 应与云端提醒 rid 一致"
    finally:
        _clear_fakes()


def test_cancel_rid_exact_match():
    # 端到端：create（镜像带 rid）→ cancel 同 rid → rid 精确删除命中，不走内容匹配
    st = _install_fakes()
    try:
        import types as _t
        conn = _t.SimpleNamespace(device_id="devE2E")
        resp = asyncio.run(nr.networked_reminder_create(conn, "喝水", {"type": "daily", "hour": 9, "minute": 0}))
        assert resp.action == nr.Action.REQLLM
        t = st["task"]
        rid = t["rid"]
        resp2 = asyncio.run(nr.networked_reminder_cancel(conn, rid))
        assert resp2.action == nr.Action.REQLLM
        assert st["deleted_by_rid"] == [rid], "cancel 应按 rid 精确删镜像"
        assert st["deleted"] == [], "rid 命中时不应再走内容匹配"
        assert st["pushed"][2] == [t["id"]], "删除应经 schedule_push deletes 下行"
        assert st["published"][1] == "delete"
    finally:
        _clear_fakes()


def test_cancel_rid_does_not_touch_same_content_other():
    # 同内容两条提醒：按 rid 取消只删目标镜像，另一条必须保留
    st = _install_fakes()
    try:
        import types as _t
        conn = _t.SimpleNamespace(device_id="devTwo")
        asyncio.run(nr.networked_reminder_create(conn, "喝水", {"type": "daily", "hour": 9, "minute": 0}))
        t1 = st["task"]
        asyncio.run(nr.networked_reminder_create(conn, "喝水", {"type": "daily", "hour": 9, "minute": 0}))
        t2 = st["task"]
        assert t1["rid"] != t2["rid"] and t1["id"] != t2["id"]
        resp = asyncio.run(nr.networked_reminder_cancel(conn, t1["rid"]))
        assert resp.action == nr.Action.REQLLM
        assert st["deleted_by_rid"] == [t1["rid"]]
        remaining = sms_mod.get_schedule_mirror_store().get_cloud_tasks("devTwo")
        assert [x["rid"] for x in remaining] == [t2["rid"]], "同内容另一条提醒的镜像必须保留"
    finally:
        _clear_fakes()


def test_cancel_falls_back_to_content_match_without_rid():
    # legacy 存量：镜像条目无 rid → cancel 回落内容匹配删除（迁移前行为保持）
    st = _install_fakes()
    try:
        asyncio.run(nr._mirror_to_device_schedule("devLeg", "吃药", {"type": "daily", "hour": 9, "minute": 0}))
        t = st["task"]
        assert "rid" not in t
        import types as _t
        conn = _t.SimpleNamespace(device_id="devLeg")
        store = nr.get_networked_reminder_store()
        rid, err = store.add("devLeg", None, "吃药", {"type": "daily", "hour": 9, "minute": 0})
        assert rid, err
        resp = asyncio.run(nr.networked_reminder_cancel(conn, rid))
        assert resp.action == nr.Action.REQLLM
        assert st["deleted_by_rid"] == []          # rid 未命中（镜像无 rid）
        assert st["deleted"] == [t["id"]], "应回落内容匹配删除"
        assert st["pushed"][2] == [t["id"]]
    finally:
        _clear_fakes()


# ---------- 字段缺失 / 异常路径 ----------

def test_mirror_daily_missing_hour_skipped():
    st = _install_fakes()
    try:
        out = asyncio.run(nr._mirror_to_device_schedule("dev4", "x", {"type": "daily", "minute": 0}))
        assert out is None
        assert st["task"] is None
    finally:
        _clear_fakes()


def test_mirror_weekly_empty_repeat_skipped():
    st = _install_fakes()
    try:
        out = asyncio.run(nr._mirror_to_device_schedule("dev5", "x", {"type": "weekly", "hour": 8, "minute": 0, "repeat": []}))
        assert out is None
        assert st["task"] is None
    finally:
        _clear_fakes()


def test_mirror_store_exception_does_not_propagate():
    # 模拟 create_cloud_task 抛异常：应被 best-effort 捕获，不阻断云端提醒创建
    orig = sms_mod.get_schedule_mirror_store
    sms_mod.get_schedule_mirror_store = lambda: (_ for _ in ()).throw(RuntimeError("store boom"))
    try:
        out = asyncio.run(nr._mirror_to_device_schedule("dev6", "吃药", {"type": "daily", "hour": 9, "minute": 0}))
        assert out is None  # 异常被吞，函数正常返回
    finally:
        sms_mod.get_schedule_mirror_store = orig


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
