"""M2 下行通道 + 上行分派 离线测试（无网络/无容器依赖）。

覆盖：
- build_schedule_push 构造正确
- push_schedule_to_device 经 send_to_device 下发（在线/离线路径都不抛、返回 bool）
- publish_cloud_change 无订阅者时 no-op 不抛
- SchedulePullMessageHandler：设备 schedule_pull → 回灌云端全量 schedule_push + stale deletes
- handleIotSchedule 路由：M1 列表 → merge_full_sync + 设备全表缓存；M2 {op} 对象 → merge_ops
"""

import asyncio
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="scheddl_")
os.environ["FUGUOAI_DATA_DIR"] = _TMP

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import json  # noqa: E402

from fuguoai_core.utils.schedule_downlink import (  # noqa: E402
    build_schedule_push,
    push_schedule_to_device,
    publish_cloud_change,
)
from fuguoai_core.utils.schedule_mirror_store import (  # noqa: E402
    get_schedule_mirror_store,
    CLOUD_ID_HIGH_BIT,
    CLOUD_ID_MAX,
    CLOUD_ID_FLOOR,
)
from fuguoai_core.handle.textHandler.schedulePullMessageHandler import (  # noqa: E402
    SchedulePullMessageHandler,
)
from fuguoai_core.providers.tools.device_iot.iot_handler import (  # noqa: E402
    handleIotSchedule,
)

DEV = "11:22:33:44:55:66"


# --------------------------------------------------------------------------- #
# 桩：fake connection / logger
# --------------------------------------------------------------------------- #
class _StubLogger:
    def bind(self, **kw):
        return self

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


class _FakeConn:
    def __init__(self, device_id):
        self.device_id = device_id
        self.sent = []
        self.logger = _StubLogger()

    async def safe_send(self, data):
        self.sent.append(data)


class _FakeReg:
    def __init__(self, result):
        self._result = result
        self.last = None

    async def send_to_device(self, device_id, message):
        self.last = (device_id, message)
        return self._result


def _reset():
    s = get_schedule_mirror_store()
    s.replace(DEV, [])
    s.save_device_table(DEV, [])   # 设备全表缓存一并清（stale deletes 数据源）


# --------------------------------------------------------------------------- #
# build_schedule_push
# --------------------------------------------------------------------------- #
def test_build_schedule_push_defaults():
    p = build_schedule_push()
    assert p == {"type": "schedule_push", "tasks": [], "deletes": []}


def test_build_schedule_push_payload():
    p = build_schedule_push(tasks=[{"id": 1}], deletes=[2])
    assert p["type"] == "schedule_push"
    assert p["tasks"] == [{"id": 1}]
    assert p["deletes"] == [2]


# --------------------------------------------------------------------------- #
# _to_device_task：小程序字符串 repeat / date → 设备 JSON 契约（数字）
# 背景：设备 JsonToTask 只认数字 repeat，非数字 fallback 0x7F=每天；
# 小程序 reminders.js 发的是 'daily'/'once'/'weekday'/'weekend' 字符串。
# --------------------------------------------------------------------------- #
def test_to_device_task_repeat_strings():
    cases = {
        "daily": 0x7F, "everyday": 0x7F, "每天": 0x7F,
        "once": 0, "单次": 0, "一次": 0,
        "weekday": 0x3E, "workday": 0x3E, "工作日": 0x3E,
        "weekend": 0x41, "周末": 0x41,
    }
    for s, mask in cases.items():
        p = build_schedule_push(tasks=[{"id": 0x80000001, "repeat": s}])
        assert p["tasks"][0]["repeat"] == mask, f"{s} 应归一为 {mask}"


def test_to_device_task_date_string():
    p = build_schedule_push(tasks=[
        {"id": 0x80000002, "repeat": "once", "date": "2026-10-20", "message": "喝水"},
        {"id": 0x80000003, "repeat": "once", "date": "2026/10/20", "message": "吃药"},
    ])
    t1, t2 = p["tasks"]
    assert (t1["date_y"], t1["date_m"], t1["date_d"]) == (2026, 10, 20)
    assert (t2["date_y"], t2["date_m"], t2["date_d"]) == (2026, 10, 20)


def test_to_device_task_numeric_passthrough():
    # 语音镜像路径发的就是设备约定位图（数字），不得二次转换
    p = build_schedule_push(tasks=[
        {"id": 0x80000004, "repeat": 42},
        {"id": 0x80000005, "repeat": 0},
        {"id": 0x80000006, "repeat": 127},
        {"id": 0x80000007, "repeat": "62"},  # 纯数字字符串 → int
    ])
    reps = [t["repeat"] for t in p["tasks"]]
    assert reps == [42, 0, 127, 62]


def test_to_device_task_unknown_repeat_untouched():
    # 无法识别的 repeat 保持原样（设备按每天处理，与历史行为一致）
    p = build_schedule_push(tasks=[{"id": 1, "repeat": "weird"}])
    assert p["tasks"][0]["repeat"] == "weird"
    # 无 repeat 字段的任务原样透传（不含注入的默认值）
    p2 = build_schedule_push(tasks=[{"id": 1}])
    assert p2["tasks"] == [{"id": 1}]


def test_to_device_task_bad_date_ignored():
    p = build_schedule_push(tasks=[{"id": 1, "repeat": "once", "date": "20-10-2026"}])
    t = p["tasks"][0]
    assert "date_y" not in t and "date" in t  # 非法日期不解析，原样保留


def test_to_device_task_keeps_source():
    # 防回归（2026-10-18 屏显修复尝试后回退）：source 必须原样透传。
    # 曾尝试下行剥离 source 让设备显示 [本·通]，实测不可行且有害：
    # ① 固件对 schedule_push 到达的任务统一标记 kCloud（8818 实证：无线
    #    source 的载荷设备仍存 source=1），剥离达不到显示目的；
    # ② HandleSchedulePush 的 deletes 只删 source==kCloud 或 id 高位的任务
    #    （device_control.cc:1626），新 id（INT_MAX 递减）无高位 bit，设备
    #    存成 kLocal 后 fired once 的下行删除会被静默跳过 ⇒ "过时间了也没
    #    清除"复发。屏显 [云] 只能固件侧改（ScheduleRowPrefix），后端不碰。
    p = build_schedule_push(tasks=[{"id": CLOUD_ID_MAX - 7, "source": "cloud",
                                    "hour": 8, "minute": 0, "repeat": 127,
                                    "message": "早餐"}])
    t = p["tasks"][0]
    assert t["source"] == "cloud", "下行必须带 source（设备 deletes 路径依赖 kCloud）"
    assert t["id"] == CLOUD_ID_MAX - 7 and t["repeat"] == 127 and t["message"] == "早餐"


def test_pull_payload_keeps_source_storage_keeps_cloud():
    # 端到端：pull 回灌载荷带 source=cloud；mirror 存储态同为 source=cloud
    _reset()
    s = get_schedule_mirror_store()
    s.create_cloud_task(DEV, {"hour": 8, "minute": 0, "repeat": 127, "message": "早餐"})
    assert s.get_cloud_tasks(DEV)[0]["source"] == "cloud"   # 存储态保留
    conn = _FakeConn(DEV)
    h = SchedulePullMessageHandler()
    asyncio.run(h.handle(conn, {"type": "schedule_pull"}))
    payload = json.loads(conn.sent[0])
    assert all(t.get("source") == "cloud" for t in payload["tasks"]), \
        "pull 载荷必须带 source=cloud（防回归：曾被误删导致 deletes 失效风险）"


# --------------------------------------------------------------------------- #
# push_schedule_to_device（在线 / 离线 / 异常都不抛，返回 bool）
# --------------------------------------------------------------------------- #
def test_push_online():
    _reset()
    reg = _FakeReg(True)
    import fuguoai_core.utils.connection_registry as cr
    orig = cr.get_connection_registry
    cr.get_connection_registry = lambda: reg
    try:
        ok = asyncio.run(push_schedule_to_device(DEV, tasks=[{"id": CLOUD_ID_HIGH_BIT}]))
    finally:
        cr.get_connection_registry = orig
    assert ok is True
    did, msg = reg.last
    from fuguoai_core.utils.util import normalize_device_id
    assert did == normalize_device_id(DEV)
    assert isinstance(msg, dict)
    assert msg["type"] == "schedule_push"
    assert msg["tasks"][0]["id"] == CLOUD_ID_HIGH_BIT


def test_push_offline_returns_false():
    _reset()
    reg = _FakeReg(False)
    import fuguoai_core.utils.connection_registry as cr
    orig = cr.get_connection_registry
    cr.get_connection_registry = lambda: reg
    try:
        ok = asyncio.run(push_schedule_to_device(DEV, deletes=[5]))
    finally:
        cr.get_connection_registry = orig
    assert ok is False


def test_push_no_device_id_returns_false():
    assert asyncio.run(push_schedule_to_device("", tasks=[{}])) is False


def test_publish_cloud_change_noop():
    # 无订阅者时 publish 为 no-op，且绝不抛
    publish_cloud_change(DEV, "create", task={"id": 1})
    publish_cloud_change(DEV, "delete", deleted_id=2)


# --------------------------------------------------------------------------- #
# SchedulePullMessageHandler：回灌云端全量
# --------------------------------------------------------------------------- #
def test_pull_handler_returns_cloud_full():
    _reset()
    s = get_schedule_mirror_store()
    s.create_cloud_task(DEV, {"message": "云端A"})
    s.merge_ops(DEV, "upsert", [{"id": 1, "source": "local", "modified_utc": 1}])  # 本地任务
    conn = _FakeConn(DEV)
    h = SchedulePullMessageHandler()
    asyncio.run(h.handle(conn, {"type": "schedule_pull"}))
    assert len(conn.sent) == 1
    payload = json.loads(conn.sent[0])
    assert payload["type"] == "schedule_push"
    assert payload["deletes"] == []
    # 只含云端任务，不含本地 id=1
    ids = {t["id"] for t in payload["tasks"]}
    assert 1 not in ids
    # 2026-10-18 起云端 id 从 INT_MAX 递减（不再 >= CLOUD_ID_HIGH_BIT）
    assert all(CLOUD_ID_FLOOR <= i <= CLOUD_ID_MAX for i in ids), ids


def test_pull_handler_includes_stale_deletes():
    # 设备表里有、mirror 已无的云端 id → pull 回灌时放进 deletes 补删
    # （once 触发/取消后设备没收到 delete 的残留，靠此差集清理）
    _reset()
    s = get_schedule_mirror_store()
    c1 = s.create_cloud_task(DEV, {"rid": "nr1", "message": "once喝水"})
    c2 = s.create_cloud_task(DEV, {"rid": "nr2", "message": "daily早安"})
    # 设备 M1 上报的全表含两条云端 + 一条本地
    s.save_device_table(DEV, [
        {"id": 1, "source": "local", "modified_utc": 1},
        {"id": c1["id"], "source": 1, "modified_utc": 1},
        {"id": c2["id"], "source": 1, "modified_utc": 1},
    ])
    # once 触发后从 mirror 删掉 c1（设备离线未知情）
    s.delete_cloud_task_by_rid(DEV, "nr1")
    conn = _FakeConn(DEV)
    h = SchedulePullMessageHandler()
    asyncio.run(h.handle(conn, {"type": "schedule_pull"}))
    payload = json.loads(conn.sent[0])
    assert payload["deletes"] == [c1["id"]], f"应补删 fired 的 c1，实际 {payload['deletes']}"
    assert [t["id"] for t in payload["tasks"]] == [c2["id"]]


def test_pull_handler_normalizes_repeat_string():
    # 小程序建的字符串 repeat 任务，pull 回灌时归一成设备位图；存储态不变
    _reset()
    s = get_schedule_mirror_store()
    s.create_cloud_task(DEV, {"repeat": "weekday", "hour": 8, "minute": 0, "message": "早餐"})
    conn = _FakeConn(DEV)
    h = SchedulePullMessageHandler()
    asyncio.run(h.handle(conn, {"type": "schedule_pull"}))
    payload = json.loads(conn.sent[0])
    assert payload["tasks"][0]["repeat"] == 0x3E  # 工作日 = 周一~周五
    assert s.get_cloud_tasks(DEV)[0]["repeat"] == "weekday"  # 存储态保持小程序原样


def test_pull_handler_no_device():
    conn = _FakeConn("")
    h = SchedulePullMessageHandler()
    asyncio.run(h.handle(conn, {"type": "schedule_pull"}))
    assert conn.sent == []  # 无 device_id 直接返回，不下发


# --------------------------------------------------------------------------- #
# handleIotSchedule 路由：M1 列表 vs M2 {op}
# --------------------------------------------------------------------------- #
def test_uplink_m1_list_routes_to_full_sync():
    _reset()
    s = get_schedule_mirror_store()
    s.create_cloud_task(DEV, {"message": "云端保住"})
    conn = _FakeConn(DEV)
    # M1：纯列表（无 op）
    asyncio.run(handleIotSchedule(conn, [{"id": 1, "source": "local", "modified_utc": 1}], True))
    all_ids = {t["id"] for t in s.get(DEV)}
    assert 1 in all_ids                      # 本地被写入
    assert any(CLOUD_ID_FLOOR <= i <= CLOUD_ID_MAX for i in all_ids)  # 云端未被覆盖


def test_uplink_m1_saves_device_table():
    # M1 全量上行另存设备原始全表（pull stale deletes 差集数据源）
    _reset()
    s = get_schedule_mirror_store()
    conn = _FakeConn(DEV)
    dev_cloud_id = CLOUD_ID_MAX - 3
    asyncio.run(handleIotSchedule(conn, [
        {"id": 1, "source": "local", "modified_utc": 1},
        {"id": dev_cloud_id, "source": 1, "modified_utc": 1},
    ], True))
    # 设备上报的 cloud 项不进 mirror（后端是云端真源）→ 差集视作孤儿待清
    assert s.get_stale_cloud_deletes(DEV) == [dev_cloud_id]
    # 后端补回同 id 云端条目（如迁移重映射落到该 id）→ 差集清零
    s.merge_ops(DEV, "upsert", [{"id": dev_cloud_id, "source": "cloud", "modified_utc": 2}])
    assert s.get_stale_cloud_deletes(DEV) == []
    # 后端再删掉 → 差集重新产出（pull 时补删设备残留）
    s.delete_cloud_task(DEV, dev_cloud_id)
    assert s.get_stale_cloud_deletes(DEV) == [dev_cloud_id]


def test_uplink_m2_dict_routes_to_merge_ops():
    _reset()
    conn = _FakeConn(DEV)
    # M2：{op:"upsert", tasks:[...]}
    asyncio.run(handleIotSchedule(conn, {"op": "upsert", "tasks": [{"id": 7, "modified_utc": 5}]}, False))
    tasks = get_schedule_mirror_store().get(DEV)
    assert any(t["id"] == 7 for t in tasks)
    # M2：{op:"delete", tasks:[...]}
    asyncio.run(handleIotSchedule(conn, {"op": "delete", "tasks": [{"id": 7}]}, False))
    assert not any(t["id"] == 7 for t in get_schedule_mirror_store().get(DEV))


def test_uplink_unknown_type_discarded():
    _reset()
    conn = _FakeConn(DEV)
    asyncio.run(handleIotSchedule(conn, "garbage", False))
    assert get_schedule_mirror_store().get(DEV) == []


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
