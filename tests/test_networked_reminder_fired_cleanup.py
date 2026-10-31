"""once 提醒触发后设备日程镜像清理单测（networked_reminder_scheduler._cleanup_fired_mirror）。

背景（2026-10-18 用户实测"过时间了也没清除"）：once 提醒触发后镜像表条目不删，
设备侧残留一条已触发的一次性日程（固件 ExpireOnceTasks 只清昨天及更早的 once）。
修复：fire_one 播报成功后，once 类型按 rid 删镜像并经 schedule_push deletes 下行；
daily/weekly 不清理；legacy 无 rid 条目不在此路径（靠 pull stale deletes 差集）；
best-effort 异常绝不影响 fire_one 主流程。

手法同 test_networked_reminder_mirror.py：stub opuslib_next（intercom_registry
模块级 import），隔离 FUGUOAI_DATA_DIR，替换 scheduler 的 _device_playable /
speak_text_to_device / store / mirror store / push。
"""

import asyncio
import os
import sys
import tempfile
import types

if "opuslib_next" not in sys.modules:
    sys.modules["opuslib_next"] = types.ModuleType("opuslib_next")

_TMP = tempfile.mkdtemp(prefix="nrfired_")
os.environ["FUGUOAI_DATA_DIR"] = _TMP

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import fuguoai_core.utils.networked_reminder_scheduler as nrs  # noqa: E402
import fuguoai_core.utils.schedule_mirror_store as sms_mod  # noqa: E402
import fuguoai_core.utils.schedule_downlink as sdl_mod  # noqa: E402


class _FakeReminderStore:
    """最小 networked_reminder_store：只实现 fire_one/_cleanup 路径用到的面。"""

    def __init__(self, items):
        self._items = items          # rid -> item dict
        self.fired = []

    def mark_fired(self, device_id, rid, now=None):
        self.fired.append(rid)
        return True

    def set_attempt(self, device_id, rid, now, kind, error=None):
        return True

    def get(self, device_id, rid):
        return self._items.get(rid)


class _FakeMirrorStore:
    def __init__(self):
        self.deleted_by_rid = []
        self.boom = False

    def delete_cloud_task_by_rid(self, device_id, rid):
        if self.boom:
            raise RuntimeError("mirror boom")
        self.deleted_by_rid.append(rid)
        return 2147483647


def _install(items, mirror=None):
    """打桩 scheduler 依赖；返回观测 state。"""
    state = {
        "playable": True,
        "spoken": [],
        "pushed": [],
        "fired": None,
    }
    reminder = _FakeReminderStore(items)
    mirror = mirror or _FakeMirrorStore()

    nrs._device_playable = lambda did: state["playable"]

    async def _fake_speak(device_id, text, label="", voice=None):
        state["spoken"].append((device_id, text, label))
        return True

    nrs.speak_text_to_device = _fake_speak
    nrs.get_networked_reminder_store = lambda: reminder

    async def _fake_push(device_id, tasks=None, deletes=None):
        state["pushed"].append((device_id, tasks, deletes))
        return True

    sms_mod.get_schedule_mirror_store = lambda: mirror
    sdl_mod.push_schedule_to_device = _fake_push
    return state, reminder, mirror


def _restore():
    import importlib
    importlib.reload(nrs)
    importlib.reload(sms_mod)
    importlib.reload(sdl_mod)


# --------------------------------------------------------------------------- #
# once 触发 → 镜像清理 + deletes 下行
# --------------------------------------------------------------------------- #
def test_once_fired_cleans_mirror_and_pushes_deletes():
    items = {"nrONCE": {"rid": "nrONCE", "schedule": {"type": "once", "fire_at": 1}}}
    state, reminder, mirror = _install(items)
    try:
        sched = nrs.get_networked_reminder_scheduler()
        ok = asyncio.run(sched.fire_one("devF", "nrONCE", "喝水", auto=False))
        assert ok is True
        assert reminder.fired == ["nrONCE"], "fire_one 仍应 mark_fired"
        assert mirror.deleted_by_rid == ["nrONCE"], "once 应按 rid 删镜像"
        assert state["pushed"] == [("devF", [], [2147483647])], \
            f"删除应经 schedule_push deletes 下行，实际 {state['pushed']}"
    finally:
        _restore()


def test_daily_fired_keeps_mirror():
    items = {"nrDAILY": {"rid": "nrDAILY", "schedule": {"type": "daily", "hour": 9, "minute": 0}}}
    state, reminder, mirror = _install(items)
    try:
        sched = nrs.get_networked_reminder_scheduler()
        ok = asyncio.run(sched.fire_one("devG", "nrDAILY", "吃药", auto=False))
        assert ok is True
        assert mirror.deleted_by_rid == [], "daily 周期性日程不删镜像"
        assert state["pushed"] == [], "无删除即无下行"
    finally:
        _restore()


def test_mirror_without_rid_is_left_to_pull_diff():
    # legacy 存量：delete_cloud_task_by_rid 返回 None（无 rid 条目）→ 不 push，
    # 由 pull 的 stale deletes 差集在下次重连时清理
    items = {"nrLEG": {"rid": "nrLEG", "schedule": {"type": "once", "fire_at": 1}}}

    class _NoHitMirror(_FakeMirrorStore):
        def delete_cloud_task_by_rid(self, device_id, rid):
            self.deleted_by_rid.append(rid)
            return None

    state, reminder, mirror = _install(items, _NoHitMirror())
    try:
        sched = nrs.get_networked_reminder_scheduler()
        ok = asyncio.run(sched.fire_one("devH", "nrLEG", "喝水", auto=False))
        assert ok is True
        assert mirror.deleted_by_rid == ["nrLEG"], "仍应尝试 rid 删除"
        assert state["pushed"] == [], "无 rid 镜像条目 → 不发 deletes（靠 pull 差集）"
    finally:
        _restore()


def test_cleanup_exception_does_not_break_fire_one():
    # 镜像删除/push 抛异常 → best-effort 吞掉，fire_one 仍返回已播报
    items = {"nrBOOM": {"rid": "nrBOOM", "schedule": {"type": "once", "fire_at": 1}}}
    mirror = _FakeMirrorStore()
    mirror.boom = True
    state, reminder, mirror = _install(items, mirror)
    try:
        sched = nrs.get_networked_reminder_scheduler()
        ok = asyncio.run(sched.fire_one("devI", "nrBOOM", "喝水", auto=False))
        assert ok is True, "镜像清理异常不得影响播报结果"
        assert reminder.fired == ["nrBOOM"]
        assert state["pushed"] == []
    finally:
        _restore()


def test_cleanup_missing_item_is_noop():
    # store.get 返回 None（极端：提醒已被删）→ 直接返回，不碰镜像
    state, reminder, mirror = _install({})
    try:
        sched = nrs.get_networked_reminder_scheduler()
        ok = asyncio.run(sched.fire_one("devJ", "nrGONE", "喝水", auto=False))
        assert ok is True
        assert mirror.deleted_by_rid == []
        assert state["pushed"] == []
    finally:
        _restore()


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
