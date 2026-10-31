import json
import os
import threading
import time

from fuguoai_core.utils.util import normalize_device_id

_DATA_DIR = os.environ.get("FUGUOAI_DATA_DIR", "/opt/fuguoai-server/data")
_PATH = os.path.join(_DATA_DIR, "schedule_mirror.json")
_DEVICE_TABLE_PATH = os.path.join(_DATA_DIR, "schedule_device_tables.json")
_lock = threading.Lock()

# 云端日程 id 命名空间（2026-10-18 修订；生产实证：cJSON valueint 溢出钳位）。
#
# 历史约定（契约 §2.3 / §4.2 / §5.2）：云端 id 从 0x80000000（2147483648）起
# 递增，靠高位 bit 与本地 id（设备 next_id_ 从 1 起）隔离。但固件 JsonToTask
# 用 cJSON 读 it->valueint（int），cJSON 对 >INT_MAX 的数把 valueint 饱和成
# INT_MAX=2147483647 ⇒ **所有 >=0x80000000 的云端 id 到设备侧全部变成
# 2147483647**：设备表里云端任务只有一个槽位，后到者按 LWW 覆盖先到者
# （2026-10-18 8818 实证：早餐→喝水10:30→喝水10:50 三次覆盖，设备永远只剩
# 1 条云端任务；deletes 按钳位 id 也可能误删）。
#
# 新约定：云端 id 从 INT_MAX（0x7FFFFFFF）**递减**分配。
# - <=INT_MAX ⇒ cJSON 不钳位，设备侧 id 与后端一致，多云端任务可共存；
# - 与本地 id 不碰撞：固件 AddTask 对无高位 bit 的 id 会推进 next_id_
#   （scheduler.h:144-149），首个 2147483647 落位后 next_id_=2147483648，
#   之后本地 id 从 2147483648 往上、云端 id 递减，两个方向永不相交；
# - 命名空间判定改用显式 source 字段（"cloud"/1），不再依赖 id 区间；
#   CLOUD_ID_HIGH_BIT 仅保留用于识别迁移前的 legacy 存量高位 id。
CLOUD_ID_MAX = 0x7FFFFFFF        # 2147483647 = INT_MAX，cJSON valueint 不钳位上限
CLOUD_ID_FLOOR = 1_000_000_000   # 云端 id 递减下限，与本地 id 空间留 1e9 余量
CLOUD_ID_HIGH_BIT = 0x80000000   # 2147483648，legacy：仅识别迁移前存量高位 id

SOURCE_LOCAL = "local"
SOURCE_CLOUD = "cloud"


def _is_cloud_task(item):
    """判定一条任务是否属于云端命名空间。

    主判据：显式 source 字段（后端所有云端条目都带 source=cloud；设备上行
    的 cloud 项带 source=1）。兜底一：id 落在 legacy 高位区间（>=0x80000000，
    仅迁移前存量）。兜底二：id 落在新区间（>=CLOUD_ID_FLOOR）——防御性规则，
    覆盖 source 字段缺失/异常的条目（如历史数据、非常规上行），保证 stale
    deletes 差集与命名空间判定不漂移。本地 id 由设备 next_id_ 从 1 递增，
    正常使用到不了 1e9，区间不碰撞。
    """
    if not isinstance(item, dict):
        return False
    src = item.get("source")
    if src in (SOURCE_CLOUD, 1, "1", "cloud"):
        return True
    try:
        return int(item.get("id", 0)) >= CLOUD_ID_FLOOR
    except (TypeError, ValueError):
        return False


def _task_id(item):
    try:
        return int(item.get("id", 0))
    except (TypeError, ValueError):
        return 0


def _mod_utc(item):
    try:
        return int(item.get("modified_utc", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _load():
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(db):
    os.makedirs(os.path.dirname(_PATH), exist_ok=True)
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _PATH)


def _load_device_tables():
    try:
        with open(_DEVICE_TABLE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_device_tables(db):
    os.makedirs(os.path.dirname(_DEVICE_TABLE_PATH), exist_ok=True)
    tmp = _DEVICE_TABLE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _DEVICE_TABLE_PATH)


class ScheduleMirrorStore:
    """设备日程镜像 + 云端日程本体（契约 Part A.7 / §5）。

    存储模型：后端以 `device_id + id` 为键存**全量**（契约 §5.3）——既包含设备
    上行镜像的本地任务（source=local），也包含由云端/小程序创建的在线日程
    （source=cloud）。两类按 source 命名空间隔离（id 区间自 2026-10-18 起
    不再作为命名空间依据，原因见模块顶部 CLOUD_ID_MAX 注释：cJSON 钳位）。

    语义要点：
    - 设备是真源（offline-first）：本地任务以设备上行为准。
    - 后端是云端任务真源：云端创建的在线日程后端拥有并下行同步到设备。
    - **绝不**用设备全量覆盖自身（解决现状问题 2）：M1 全量上行仅替换本地子集、
      保留云端任务（见 merge_full_sync）；M2 增量上行按 id 做 LWW merge（见 merge_ops）。
    - 设备上报的原始全表另存 data/schedule_device_tables.json（save_device_table），
      仅用于 pull 回灌时计算 stale deletes 差集（设备表有、mirror 无的云端 id）。
    - 持久化到 data/schedule_mirror.json（主机卷），重启不丢。
    """

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def get(self, device_id):
        """返回该设备全部任务（本地 + 云端）的列表，供小程序「我的提醒」展示。"""
        did = normalize_device_id(device_id)
        with _lock:
            db = _load()
            return db.get(did, [])

    def get_local_tasks(self, device_id):
        """仅本地命名空间任务（source=local）。"""
        return [it for it in self.get(device_id) if not _is_cloud_task(it)]

    def get_cloud_tasks(self, device_id):
        """仅云端命名空间任务（source=cloud），供下行 pull/push 下发。"""
        return [it for it in self.get(device_id) if _is_cloud_task(it)]

    # ------------------------------------------------------------------ #
    # 写入：遗留整体覆盖（仅测试/管理重置用，正常上行走下方 merge_*）
    # ------------------------------------------------------------------ #
    def replace(self, device_id, items):
        """整体覆盖（legacy full replace）：无差别替换该设备整张表。

        ⚠️ 仅用于迁移/测试/管理重置。正常上行请走 merge_full_sync / merge_ops，
        否则会抹掉后端拥有的云端任务。
        """
        if not isinstance(items, list):
            return 0
        did = normalize_device_id(device_id)
        cleaned = [it for it in items if isinstance(it, dict)]
        with _lock:
            db = _load()
            if cleaned:
                db[did] = cleaned
            else:
                db.pop(did, None)
            _save(db)
        return len(cleaned)

    # ------------------------------------------------------------------ #
    # 写入：M1 全量上行（安全合并，保留云端任务）
    # ------------------------------------------------------------------ #
    def merge_full_sync(self, device_id, items):
        """M1 全量数组上行（兼容旧后端整体覆盖语义，但**保留云端任务**）。

        设备是真源，但只拥有本地命名空间（source=local）。因此：
        - 保留 store 中既有云端任务（后端真源，不被设备全量抹掉）；
        - 仅用设备上行的本地任务**替换本地子集**。
        设备若误带云端 id 的项（不应发生，M1 无下行），直接忽略以保后端云端真源。
        返回 (n_local, n_cloud) 供日志观测。
        """
        if not isinstance(items, list):
            return (0, 0)
        did = normalize_device_id(device_id)
        with _lock:
            db = _load()
            existing = {_task_id(it): it for it in db.get(did, []) if isinstance(it, dict)}
            cloud = {iid: it for iid, it in existing.items() if _is_cloud_task(it)}
            local_new = {
                _task_id(it): it
                for it in items
                if isinstance(it, dict) and not _is_cloud_task(it)
            }
            merged = {}
            merged.update(local_new)
            merged.update(cloud)  # 云端真源优先，不被本地子集覆盖
            db[did] = list(merged.values())
            _save(db)
            return (len(local_new), len(cloud))

    # ------------------------------------------------------------------ #
    # 写入：M2 增量上行（按 op 做 LWW merge / tombstone）
    # ------------------------------------------------------------------ #
    def merge_ops(self, device_id, op, tasks):
        """M2 增量上行（契约 §5.1）：op ∈ {upsert, delete}，tasks 为任务列表。

        - upsert：按 id LWW 合并（modified_utc 新者胜；相等则云端胜，避免本地脏写覆盖云端）。
        - delete：按 id 移除（含本地 / 云端 id；云端 id 的删除即从设备侧向上传播删除，
          符合 offline-first：离线删的云端日程联网后由上行传播，后端不再下发 → 防复活）。
        返回 (n_upserted, n_deleted)。
        """
        did = normalize_device_id(device_id)
        if not isinstance(tasks, list):
            tasks = []
        with _lock:
            db = _load()
            existing = {_task_id(it): it for it in db.get(did, []) if isinstance(it, dict)}
            n_up, n_del = 0, 0
            if op == "upsert":
                for it in tasks:
                    if not isinstance(it, dict):
                        continue
                    iid = _task_id(it)
                    if iid == 0:
                        continue
                    prev = existing.get(iid)
                    if prev is None:
                        existing[iid] = it
                        n_up += 1
                    else:
                        # LWW：新者胜；相等则云端（真源）胜
                        if _mod_utc(it) > _mod_utc(prev):
                            existing[iid] = it
                            n_up += 1
                        elif _mod_utc(it) == _mod_utc(prev) and _is_cloud_task(it) and not _is_cloud_task(prev):
                            existing[iid] = it
                            n_up += 1
                        # 否则保留 prev（云端真源 / 较新一方）
            elif op == "delete":
                for it in tasks:
                    iid = it if isinstance(it, int) else _task_id(it if isinstance(it, dict) else {})
                    if iid == 0 and isinstance(it, dict):
                        iid = _task_id(it)
                    if iid and iid in existing:
                        del existing[iid]
                        n_del += 1
            else:
                # 未知 op：视为无操作，不破坏数据
                return (0, 0)
            db[did] = list(existing.values())
            _save(db)
            return (n_up, n_del)

    # ------------------------------------------------------------------ #
    # 云端日程本体 CRUD（小程序 / 后端创建在线日程，契约 §3.2 / §5.2）
    # ------------------------------------------------------------------ #
    def create_cloud_task(self, device_id, fields):
        """创建一条云端在线日程：分配云端 id、source=cloud、modified_utc=now。

        id 从 INT_MAX（2147483647）**递减**分配（2026-10-18 修订，原因见模块
        顶部注释：旧高位递增分配会被固件 cJSON valueint 钳成 2147483647，
        导致设备侧所有云端任务挤一个槽位互相覆盖）。取现有云端 id 的最小值
        减一；无存量时从 INT_MAX 起。递减到 CLOUD_ID_FLOOR 以下视为空间耗尽，
        抛 ValueError（调用方 best-effort 捕获，不影响云端提醒本体）。

        fields 为小程序/后端传入的业务字段（hour/minute/repeat/message/date_*/
        light/rid/...）。返回最终落盘的任务 dict。
        """
        did = normalize_device_id(device_id)
        if not isinstance(fields, dict):
            fields = {}
        with _lock:
            db = _load()
            existing = {_task_id(it): it for it in db.get(did, []) if isinstance(it, dict)}
            cloud_ids = [iid for iid in existing if _is_cloud_task(existing[iid])]
            new_id = min([CLOUD_ID_MAX + 1] + cloud_ids) - 1
            if new_id < CLOUD_ID_FLOOR:
                raise ValueError(
                    f"cloud id 空间耗尽（< {CLOUD_ID_FLOOR}），device={did}"
                )
            task = dict(fields)
            task["id"] = new_id
            task["source"] = SOURCE_CLOUD
            task["modified_utc"] = int(time.time())
            existing[new_id] = task
            db[did] = list(existing.values())
            _save(db)
            return task

    def delete_cloud_task(self, device_id, task_id):
        """删除一条云端日程（小程序删除在线日程）。返回是否真的删除了。"""
        did = normalize_device_id(device_id)
        try:
            iid = int(task_id)
        except (TypeError, ValueError):
            return False
        with _lock:
            db = _load()
            existing = {_task_id(it): it for it in db.get(did, []) if isinstance(it, dict)}
            target = existing.get(iid)
            if target is None or not _is_cloud_task(target):
                return False
            del existing[iid]
            db[did] = list(existing.values())
            _save(db)
            return True

    def delete_cloud_task_by_rid(self, device_id, rid):
        """按 rid 删除云端任务（提醒触发/取消后清理镜像用）。返回被删的 id 或 None。

        仅匹配带 rid 字段的新镜像条目（2026-10-18 起 create 时写入）；迁移前
        的无 rid 存量条目不在此列，靠 get_stale_cloud_deletes 差集在下次
        pull 时清理。
        """
        did = normalize_device_id(device_id)
        if not rid:
            return None
        with _lock:
            db = _load()
            existing = {_task_id(it): it for it in db.get(did, []) if isinstance(it, dict)}
            hit = None
            for iid, it in existing.items():
                if _is_cloud_task(it) and it.get("rid") == rid:
                    hit = iid
                    break
            if hit is None:
                return None
            del existing[hit]
            db[did] = list(existing.values())
            _save(db)
            return hit

    # ------------------------------------------------------------------ #
    # 设备实际上行全表缓存（pull stale deletes 差集用，不是 mirror 本体）
    # ------------------------------------------------------------------ #
    def save_device_table(self, device_id, items):
        """原样记录设备 M1 全量上报的 schedule 数组（含 cloud 项）。

        与 merge_full_sync 的分工：merge_full_sync 只把本地子集合进 mirror；
        本方法额外保存设备上报的**原始全表**，供 pull 回灌时计算
        「设备表里有、mirror 里没有」的云端 id（stale deletes）——fired/
        取消后从 mirror 删掉的条目，设备没收到 delete 时会残留，必须靠这个
        差集在下次重连 pull 时补删。数据存 data/schedule_device_tables.json。
        """
        did = normalize_device_id(device_id)
        cleaned = (
            [it for it in items if isinstance(it, dict)]
            if isinstance(items, list) else []
        )
        with _lock:
            db = _load_device_tables()
            db[did] = cleaned
            _save_device_tables(db)

    def get_stale_cloud_deletes(self, device_id):
        """设备表中存在、但 mirror 已无的云端 id（升序；pull 时放进 deletes）。

        覆盖：提醒 fired/cancel 后从 mirror 删除的条目、id 重映射迁移前的
        legacy 残留。本地 id 不在其列（设备本地任务由设备自身为真源）。
        """
        did = normalize_device_id(device_id)
        with _lock:
            dev_cloud = {
                _task_id(it)
                for it in _load_device_tables().get(did, [])
                if isinstance(it, dict) and _is_cloud_task(it)
            }
            db = _load()
            mirror_cloud = {
                _task_id(it)
                for it in db.get(did, [])
                if isinstance(it, dict) and _is_cloud_task(it)
            }
        return sorted(iid for iid in dev_cloud - mirror_cloud if iid)

    def update_cloud_task(self, device_id, task_id, fields):
        """更新一条云端日程的业务字段（保持 id / source / 刷新 modified_utc）。

        返回更新后的任务 dict；若 id 不存在或非云端任务返回 None。
        """
        did = normalize_device_id(device_id)
        try:
            iid = int(task_id)
        except (TypeError, ValueError):
            return None
        if not isinstance(fields, dict):
            fields = {}
        with _lock:
            db = _load()
            existing = {_task_id(it): it for it in db.get(did, []) if isinstance(it, dict)}
            target = existing.get(iid)
            if target is None or not _is_cloud_task(target):
                return None
            updated = dict(target)
            for k, v in fields.items():
                if k in ("id", "source"):
                    continue  # 保护命名空间
                updated[k] = v
            updated["modified_utc"] = int(time.time())
            existing[iid] = updated
            db[did] = list(existing.values())
            _save(db)
            return updated


_store = None


def get_schedule_mirror_store():
    global _store
    if _store is None:
        _store = ScheduleMirrorStore()
    return _store
