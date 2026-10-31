"""日程镜像 / 云端日程存储离线测试（无网络/无容器依赖，纯逻辑 + JSON 持久化）。

覆盖 M2 后端契约 §5.3 与 §4.3 的关键路径：
- M1 全量上行安全合并（保留云端任务，防镜像丢失窗口）
- M2 增量上行 LWW merge（新者胜 / 相等云端胜）
- M2 delete tombstone（云端删除不被设备全量复活）
- 云端日程 CRUD（INT_MAX 递减 id 分配 / source=cloud / modified_utc 刷新 / 不可删本地）
- get / get_local_tasks / get_cloud_tasks 拆分
- 设备全表缓存 + pull stale deletes 差集（2026-10-18）
- delete_cloud_task_by_rid（once 提醒触发后清镜像）
"""

import os
import sys
import tempfile

# 隔离数据目录，避免污染真数据（必须在 import 存储模块前设置）
_TMP = tempfile.mkdtemp(prefix="schedtest_")
os.environ["FUGUOAI_DATA_DIR"] = _TMP

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from fuguoai_core.utils.schedule_mirror_store import (  # noqa: E402
    ScheduleMirrorStore,
    get_schedule_mirror_store,
    CLOUD_ID_HIGH_BIT,
    CLOUD_ID_MAX,
    CLOUD_ID_FLOOR,
    SOURCE_CLOUD,
    SOURCE_LOCAL,
    _is_cloud_task,
    _task_id,
    _mod_utc,
)

DEV = "aa:bb:cc:dd:ee:ff"


def store():
    return get_schedule_mirror_store()


def reset(dev=DEV):
    store().replace(dev, [])
    # 同时清设备全表缓存（pull stale deletes 差集的数据源，避免用例间串扰）
    store().save_device_table(dev, [])


# --------------------------------------------------------------------------- #
# 基础判定
# --------------------------------------------------------------------------- #
def test_constants():
    # 2026-10-18 起云端 id 从 INT_MAX 递减分配（cJSON valueint 不钳位上限）
    assert CLOUD_ID_MAX == 0x7FFFFFFF
    assert CLOUD_ID_FLOOR == 1_000_000_000
    assert CLOUD_ID_FLOOR < CLOUD_ID_MAX
    # CLOUD_ID_HIGH_BIT 降级为仅识别迁移前 legacy 存量高位 id
    assert CLOUD_ID_HIGH_BIT == 0x80000000
    assert CLOUD_ID_HIGH_BIT > CLOUD_ID_MAX


def test_is_cloud_task():
    # 主判据：显式 source 字段
    assert _is_cloud_task({"id": 1, "source": SOURCE_CLOUD}) is True
    assert _is_cloud_task({"id": 1, "source": 1}) is True       # 设备上行用数字 1
    assert _is_cloud_task({"id": 1, "source": "1"}) is True
    assert _is_cloud_task({"id": 1, "source": "cloud"}) is True
    assert _is_cloud_task({"id": 1, "source": SOURCE_LOCAL}) is False
    # 兜底二（2026-10-18 屏显修复）：新区间 id（>=CLOUD_ID_FLOOR）无 source 也认云端
    # ——下行态已剥离 source（终端用户不应看到 [云] 标签），设备 M1 上报的 cloud
    # 项是 source=0，必须靠 id 区间识别，否则 stale deletes 差集失效。
    assert _is_cloud_task({"id": CLOUD_ID_MAX - 3}) is True
    assert _is_cloud_task({"id": CLOUD_ID_MAX - 3, "source": 0}) is True
    assert _is_cloud_task({"id": CLOUD_ID_FLOOR}) is True
    # 本地 id（设备 next_id_ 从 1 递增，到不了 1e9）无 source ⇒ 非云端
    assert _is_cloud_task({"id": 1}) is False
    assert _is_cloud_task({"id": CLOUD_ID_FLOOR - 1}) is False
    assert _is_cloud_task({"id": 999_999_999, "source": 0}) is False
    # 兜底一：legacy 迁移前存量高位 id
    assert _is_cloud_task({"id": CLOUD_ID_HIGH_BIT}) is True
    assert _is_cloud_task({"id": CLOUD_ID_HIGH_BIT + 5}) is True
    assert _is_cloud_task("not a dict") is False


def test_task_id_mod_utc():
    assert _task_id({"id": 42}) == 42
    assert _mod_utc({"modified_utc": 99}) == 99
    assert _mod_utc({}) == 0


# --------------------------------------------------------------------------- #
# M1 全量上行：替换本地子集 + 保留云端任务
# --------------------------------------------------------------------------- #
def test_m1_full_sync_replaces_local_and_keeps_cloud():
    reset()
    s = store()
    # 后端先有云端任务（小程序创建）
    cloud = s.create_cloud_task(DEV, {"hour": 8, "minute": 0, "message": "云端早安"})
    assert _is_cloud_task(cloud)
    # 设备 M1 全量上行：只含本地任务（id < 高位）
    locals_up = [
        {"id": 1, "source": SOURCE_LOCAL, "modified_utc": 100, "message": "本地A"},
        {"id": 2, "source": SOURCE_LOCAL, "modified_utc": 100, "message": "本地B"},
    ]
    n_local, n_cloud = s.merge_full_sync(DEV, locals_up)
    assert n_local == 2 and n_cloud == 1
    all_tasks = s.get(DEV)
    ids = {_task_id(t) for t in all_tasks}
    assert cloud["id"] in ids          # 云端任务被保留
    assert {1, 2}.issubset(ids)        # 本地任务被写入
    # 云端任务内容未被设备覆盖
    kept_cloud = [t for t in all_tasks if t["id"] == cloud["id"]][0]
    assert kept_cloud["message"] == "云端早安"


def test_m1_full_sync_drops_stale_local():
    reset()
    s = store()
    s.merge_full_sync(DEV, [{"id": 1, "source": SOURCE_LOCAL, "modified_utc": 1, "message": "旧"}])
    # 设备下次全量上行不再含 id=1 → 应被移除
    s.merge_full_sync(DEV, [{"id": 2, "source": SOURCE_LOCAL, "modified_utc": 1, "message": "新"}])
    ids = {_task_id(t) for t in s.get(DEV)}
    assert ids == {2}


def test_m1_full_sync_ignores_device_cloud_ids():
    reset()
    s = store()
    s.create_cloud_task(DEV, {"message": "真云端"})
    # 设备误带 source=1 的云端项（M1 本不应有）：后端忽略，保留自身云端真源
    s.merge_full_sync(DEV, [{"id": CLOUD_ID_MAX - 100, "source": 1, "message": "设备伪造云端"}])
    # 屏显修复后下行不带 source：设备上报的 cloud 项是 source=0，靠 id 区间识别
    s.merge_full_sync(DEV, [{"id": CLOUD_ID_MAX - 101, "source": 0, "message": "设备伪造云端2"}])
    # legacy 高位 id 无 source 也认（迁移前存量），同样不被本地子集吞掉
    s.merge_full_sync(DEV, [{"id": CLOUD_ID_HIGH_BIT, "message": "设备伪造legacy"}])
    cloud = s.get_cloud_tasks(DEV)
    assert len(cloud) == 1
    assert cloud[0]["message"] == "真云端"
    # 低位 id 无 source 视为本地任务（可被设备替换）
    s.merge_full_sync(DEV, [{"id": 5, "modified_utc": 1, "message": "本地5"}])
    assert [t for t in s.get_local_tasks(DEV) if t["id"] == 5]


def test_m1_device_cloud_source0_stale_deletes():
    # 屏显修复闭环：设备 M1 上报 source=0 + 新区间 id（下行剥离 source 的结果）
    # ⇒ 仍识别为云端；mirror 删掉后 stale deletes 差集必须能补删
    reset()
    s = store()
    cid = CLOUD_ID_MAX - 50
    s.merge_ops(DEV, "upsert", [{"id": cid, "source": SOURCE_CLOUD, "modified_utc": 5}])
    s.save_device_table(DEV, [
        {"id": 1, "source": "local", "modified_utc": 5},
        {"id": cid, "source": 0, "modified_utc": 5},   # 设备视角：无来源标签
    ])
    assert s.get_stale_cloud_deletes(DEV) == []          # 双边都在 ⇒ 无 stale
    s.delete_cloud_task(DEV, cid)                        # 后端删除（fired/cancel）
    assert s.get_stale_cloud_deletes(DEV) == [cid]       # 差集补删仍生效


# --------------------------------------------------------------------------- #
# M2 增量上行：LWW merge
# --------------------------------------------------------------------------- #
def test_m2_upsert_lww_newer_wins():
    reset()
    s = store()
    s.merge_ops(DEV, "upsert", [{"id": 1, "modified_utc": 100, "message": "v1"}])
    # 旧版本上行 → 不应覆盖
    s.merge_ops(DEV, "upsert", [{"id": 1, "modified_utc": 50, "message": "v0-stale"}])
    t = [x for x in s.get(DEV) if x["id"] == 1][0]
    assert t["message"] == "v1" and t["modified_utc"] == 100
    # 新版本上行 → 覆盖
    s.merge_ops(DEV, "upsert", [{"id": 1, "modified_utc": 200, "message": "v2"}])
    t = [x for x in s.get(DEV) if x["id"] == 1][0]
    assert t["message"] == "v2" and t["modified_utc"] == 200


def test_m2_upsert_equal_mod_cloud_wins():
    reset()
    s = store()
    # 后端已有本地任务 modified_utc=100
    s.merge_ops(DEV, "upsert", [{"id": 1, "source": SOURCE_LOCAL, "modified_utc": 100, "message": "local"}])
    # 设备上行同 modified_utc 但声明为云端 → 云端（真源）胜
    s.merge_ops(DEV, "upsert", [{"id": 1, "source": SOURCE_CLOUD, "modified_utc": 100, "message": "cloud"}])
    t = [x for x in s.get(DEV) if x["id"] == 1][0]
    assert t["message"] == "cloud" and t["source"] == SOURCE_CLOUD


def test_m2_upsert_mixed_batch():
    reset()
    s = store()
    s.merge_ops(DEV, "upsert", [
        {"id": 1, "modified_utc": 10, "message": "a"},
        {"id": 2, "modified_utc": 10, "message": "b"},
    ])
    assert len(s.get(DEV)) == 2


def test_m2_delete_removes_by_id():
    reset()
    s = store()
    s.merge_ops(DEV, "upsert", [{"id": 1, "modified_utc": 10, "message": "a"}])
    n_up, n_del = s.merge_ops(DEV, "delete", [{"id": 1}])
    assert n_up == 0 and n_del == 1
    assert s.get(DEV) == []
    # 删除不存在的 id → 不计
    n_up, n_del = s.merge_ops(DEV, "delete", [{"id": 999}])
    assert n_del == 0


def test_unknown_op_is_noop():
    reset()
    s = store()
    s.merge_ops(DEV, "upsert", [{"id": 1, "modified_utc": 10}])
    n_up, n_del = s.merge_ops(DEV, "bogus", [{"id": 1, "modified_utc": 999}])
    assert (n_up, n_del) == (0, 0)
    assert [x for x in s.get(DEV) if x["id"] == 1][0]["modified_utc"] == 10


# --------------------------------------------------------------------------- #
# 云端删除防复活（§4.3：云端 delete 优先，即便设备本地仍有/全量上行也不复活）
# --------------------------------------------------------------------------- #
def test_cloud_delete_survives_device_resync():
    reset()
    s = store()
    cloud = s.create_cloud_task(DEV, {"message": "待删云端"})
    assert s.delete_cloud_task(DEV, cloud["id"]) is True
    # 设备随后 M1 全量上行（只含本地任务，不含该云端 id）→ 云端删除不被复活
    s.merge_full_sync(DEV, [{"id": 1, "source": SOURCE_LOCAL, "modified_utc": 1, "message": "本地"}])
    cloud_ids = {_task_id(t) for t in s.get_cloud_tasks(DEV)}
    assert cloud["id"] not in cloud_ids


def test_device_delete_op_propagates_cloud_delete():
    reset()
    s = store()
    cloud = s.create_cloud_task(DEV, {"message": "设备侧删"})
    # 设备离线删了云端日程，联网后上行 delete（携带云端 id）
    n_up, n_del = s.merge_ops(DEV, "delete", [{"id": cloud["id"]}])
    assert n_del == 1
    assert s.get_cloud_tasks(DEV) == []


# --------------------------------------------------------------------------- #
# 云端日程 CRUD
# --------------------------------------------------------------------------- #
def test_create_cloud_task_assigns_descending_ids():
    # 2026-10-18：从 INT_MAX 递减分配（cJSON valueint 对 >INT_MAX 钳成 INT_MAX，
    # 旧的高位递增方案导致设备侧所有云端 id 全变 2147483647 互相覆盖）
    reset()
    s = store()
    c1 = s.create_cloud_task(DEV, {"hour": 7, "message": "c1"})
    c2 = s.create_cloud_task(DEV, {"hour": 8, "message": "c2"})
    c3 = s.create_cloud_task(DEV, {"hour": 9, "message": "c3"})
    assert c1["source"] == SOURCE_CLOUD
    assert c1["id"] == CLOUD_ID_MAX
    assert c2["id"] == CLOUD_ID_MAX - 1
    assert c3["id"] == CLOUD_ID_MAX - 2        # 递减，且都不超 INT_MAX（不钳位）
    assert c1["modified_utc"] > 0
    assert _is_cloud_task(c1) and _is_cloud_task(c2) and _is_cloud_task(c3)


def test_create_cloud_task_descends_across_local_and_legacy():
    reset()
    s = store()
    # 本地任务占低位
    s.merge_ops(DEV, "upsert", [{"id": 1, "source": SOURCE_LOCAL, "modified_utc": 1}])
    s.merge_ops(DEV, "upsert", [{"id": 2, "source": SOURCE_LOCAL, "modified_utc": 1}])
    c1 = s.create_cloud_task(DEV, {})
    # legacy 高位存量也应纳入最小值计算（其后新 id 从存量最小值-1 继续递减）
    s.merge_ops(DEV, "upsert", [{"id": CLOUD_ID_HIGH_BIT, "modified_utc": 1, "message": "legacy"}])
    c2 = s.create_cloud_task(DEV, {})
    assert c1["id"] == CLOUD_ID_MAX
    assert c2["id"] == CLOUD_ID_MAX - 1
    # 本地 id 绝不被云端分配器复用
    assert {c1["id"], c2["id"]}.isdisjoint({1, 2})


def test_create_cloud_task_floor_exhaustion():
    reset()
    s = store()
    # 预置一条贴着下限的云端任务 → 再创建应抛 ValueError（调用方 best-effort）
    s.replace(DEV, [{"id": CLOUD_ID_FLOOR, "source": SOURCE_CLOUD, "modified_utc": 1}])
    raised = False
    try:
        s.create_cloud_task(DEV, {"message": "应失败"})
    except ValueError:
        raised = True
    assert raised
    # 空间未耗尽（下限之上还有一格）时正常递减
    s.replace(DEV, [{"id": CLOUD_ID_FLOOR + 1, "source": SOURCE_CLOUD, "modified_utc": 1}])
    c = s.create_cloud_task(DEV, {"message": "ok"})
    assert c["id"] == CLOUD_ID_FLOOR


def test_update_cloud_task():
    reset()
    s = store()
    c = s.create_cloud_task(DEV, {"hour": 7, "message": "old"})
    updated = s.update_cloud_task(DEV, c["id"], {"message": "new", "hour": 9})
    assert updated is not None
    assert updated["message"] == "new" and updated["hour"] == 9
    assert updated["id"] == c["id"] and updated["source"] == SOURCE_CLOUD
    assert updated["modified_utc"] >= c["modified_utc"]


def test_update_cloud_task_rejects_local():
    reset()
    s = store()
    s.merge_ops(DEV, "upsert", [{"id": 1, "source": SOURCE_LOCAL, "modified_utc": 1}])
    assert s.update_cloud_task(DEV, 1, {"message": "x"}) is None  # 本地任务不可走云端 API


def test_delete_cloud_task_rejects_local():
    reset()
    s = store()
    s.merge_ops(DEV, "upsert", [{"id": 1, "source": SOURCE_LOCAL, "modified_utc": 1}])
    assert s.delete_cloud_task(DEV, 1) is False  # 本地任务不可走云端 API 删


def test_get_splits_local_cloud():
    reset()
    s = store()
    s.merge_ops(DEV, "upsert", [{"id": 1, "source": SOURCE_LOCAL, "modified_utc": 1}])
    s.create_cloud_task(DEV, {"message": "c"})
    locals_ = s.get_local_tasks(DEV)
    clouds = s.get_cloud_tasks(DEV)
    assert len(locals_) == 1 and locals_[0]["id"] == 1
    assert len(clouds) == 1 and _is_cloud_task(clouds[0])
    assert len(s.get(DEV)) == 2


# --------------------------------------------------------------------------- #
# delete_cloud_task_by_rid：once 提醒触发/取消后按 rid 清镜像（2026-10-18）
# --------------------------------------------------------------------------- #
def test_delete_cloud_task_by_rid():
    reset()
    s = store()
    c1 = s.create_cloud_task(DEV, {"rid": "nr111", "message": "once喝水", "repeat": 0})
    c2 = s.create_cloud_task(DEV, {"rid": "nr222", "message": "daily早安"})
    # 按 rid 精确删除，返回被删 id
    assert s.delete_cloud_task_by_rid(DEV, "nr111") == c1["id"]
    remaining = s.get_cloud_tasks(DEV)
    assert len(remaining) == 1 and remaining[0]["rid"] == "nr222"
    # 重复删除 / 不存在的 rid → None，不影响其他条目
    assert s.delete_cloud_task_by_rid(DEV, "nr111") is None
    assert s.delete_cloud_task_by_rid(DEV, "nope") is None
    assert s.delete_cloud_task_by_rid(DEV, "") is None
    assert s.delete_cloud_task_by_rid(DEV, None) is None
    # 仅匹配带 rid 的云端条目；本地任务即使 rid 相同也不被动
    s.merge_ops(DEV, "upsert", [{"id": 7, "source": SOURCE_LOCAL, "rid": "nr222", "modified_utc": 1}])
    assert s.delete_cloud_task_by_rid(DEV, "nr222") == c2["id"]
    assert any(t["id"] == 7 for t in s.get_local_tasks(DEV))


# --------------------------------------------------------------------------- #
# 设备全表缓存 + pull stale deletes 差集（2026-10-18）
# --------------------------------------------------------------------------- #
def test_save_device_table_and_stale_deletes():
    reset()
    s = store()
    c1 = s.create_cloud_task(DEV, {"rid": "nr111", "message": "once喝水"})
    c2 = s.create_cloud_task(DEV, {"rid": "nr222", "message": "daily早安"})
    # 设备 M1 上报的全表：本地 1/2 + 云端 c1/c2（设备侧 id 与后端一致）
    s.save_device_table(DEV, [
        {"id": 1, "source": SOURCE_LOCAL, "modified_utc": 5},
        {"id": 2, "source": SOURCE_LOCAL, "modified_utc": 5},
        {"id": c1["id"], "source": 1, "modified_utc": 5},
        {"id": c2["id"], "source": 1, "modified_utc": 5},
    ])
    # mirror 与设备表一致 → 无 stale
    assert s.get_stale_cloud_deletes(DEV) == []
    # once 触发后从 mirror 删除 c1，但设备不知道（离线）→ 差集应产出 c1
    s.delete_cloud_task_by_rid(DEV, "nr111")
    assert s.get_stale_cloud_deletes(DEV) == [c1["id"]]
    # 本地 id 不进差集（设备是真源）
    s.save_device_table(DEV, [{"id": 3, "source": SOURCE_LOCAL, "modified_utc": 9}])
    assert s.get_stale_cloud_deletes(DEV) == []


def test_stale_deletes_covers_legacy_high_ids():
    reset()
    s = store()
    # 设备表里有迁移前 legacy 高位 id，mirror 已无 → 应进 deletes 重推清理
    s.save_device_table(DEV, [{"id": CLOUD_ID_HIGH_BIT, "modified_utc": 1}])
    assert s.get_stale_cloud_deletes(DEV) == [CLOUD_ID_HIGH_BIT]


def test_device_tables_isolated_per_device():
    reset()
    reset("bb:cc:dd:ee:ff:00")
    s = store()
    c = s.create_cloud_task(DEV, {"rid": "nr111"})
    s.save_device_table(DEV, [{"id": c["id"], "source": 1}])
    s.save_device_table("bb:cc:dd:ee:ff:00", [{"id": 999, "source": 1}])
    assert s.get_stale_cloud_deletes(DEV) == []          # 另一设备不影响本设备
    assert s.get_stale_cloud_deletes("bb:cc:dd:ee:ff:00") == [999]


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
