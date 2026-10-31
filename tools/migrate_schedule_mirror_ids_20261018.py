"""存量 schedule_mirror.json 迁移：legacy 高位 cloud id → INT_MAX-100 起递减区间（2026-10-18）。

背景（docs/SCHEDULE_SYNC_ID_NAMESPACE_20261018.md §2）：
固件 cJSON 对 >INT_MAX 的数把 valueint 钳成 2147483647，旧约定云端 id 从
0x80000000 起递增 ⇒ 设备侧所有云端 id 全变同一个值、互相 LWW 覆盖。
新约定从 INT_MAX 递减分配。本脚本把存量 mirror 数据迁到新命名空间：

1. 备份 data/schedule_mirror.json → schedule_mirror.json.bak-<ts>（apply 才写备份）；
2. cloud 条目按 modified_utc 降序重映射：从 MIGRATE_TOP（INT_MAX-100）起递减；
   —— 不用 INT_MAX 本身：设备侧被钳位的 legacy id 恰是 2147483647，留一档
   头部余量，防"同帧 deletes+add 同 id"的时序歧义；
3. bump modified_utc = now（固件 HandleSchedulePush 的 LWW 严格递增才更新）；
4. 删 once 且目标时刻 <= now 的条目（与固件屏幕规则一致：过点 once 设备已隐藏，
   留着会被每次 pull 反复重推；无日期字段的 once 按 mod_utc 北京日期推定）；
5. 补 source="cloud" 字段（新命名空间判定依据）。

本地任务（source=local / 无高位 id 无 source）一字不动。

用法（两段式，先干跑后 apply）：
    python3 tools/migrate_schedule_mirror_ids_20261018.py            # dry-run，只打印计划
    python3 tools/migrate_schedule_mirror_ids_20261018.py --apply    # 备份 + 落盘

数据目录取 FUGUOAI_DATA_DIR（默认 /opt/fuguoai-server/data，与存储模块一致）。
铁律：迁移后必须重启容器或确认无并发写；改前改后各跑一次本脚本 dry-run 对账。
"""

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from fuguoai_core.utils.schedule_mirror_store import (  # noqa: E402
    CLOUD_ID_MAX,
    CLOUD_ID_FLOOR,
    CLOUD_ID_HIGH_BIT,
    SOURCE_CLOUD,
    _is_cloud_task,
)

_DATA_DIR = os.environ.get("FUGUOAI_DATA_DIR", "/opt/fuguoai-server/data")
_PATH = os.path.join(_DATA_DIR, "schedule_mirror.json")

# 迁移区间顶部：INT_MAX-100。见模块 docstring 第 2 条。
MIGRATE_TOP = CLOUD_ID_MAX - 100

_BJ = timezone(timedelta(hours=8))


def _load():
    with open(_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(db):
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _PATH)


def _task_id(it):
    try:
        return int(it.get("id", 0))
    except (TypeError, ValueError):
        return 0


def _mod_utc(it):
    try:
        return int(it.get("modified_utc", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _once_target_datetime(it, now):
    """once 任务的目标时刻（北京墙钟 datetime）；无法确定返回 None。

    优先级：date_y/date_m/date_d + hour:minute → "YYYY-MM-DD" 字符串 + hour:minute
    → 无日期字段时按创建日推定（mod_utc 的北京日期 + hour:minute——mirror 条目
    的 mod 即创建/上次更新时刻，历史上 create 后不久设备即隐藏，推定日即当天）。
    """
    h = it.get("hour")
    m = it.get("minute")
    if not (isinstance(h, int) and isinstance(m, int)):
        return None
    y, mo, d = it.get("date_y"), it.get("date_m"), it.get("date_d")
    if all(isinstance(v, int) for v in (y, mo, d)) and 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
        try:
            return datetime(y, mo, d, h, m, tzinfo=_BJ)
        except ValueError:
            return None
    s = it.get("date")
    if isinstance(s, str) and len(s) >= 10:
        try:
            dd = datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=_BJ)
            return dd.replace(hour=h, minute=m)
        except ValueError:
            return None
    # 无日期字段：按 mod_utc 北京日期推定（b4b0 的 'once' 字符串存量即此形态）
    mod = _mod_utc(it)
    if mod > 0:
        bj = datetime.fromtimestamp(mod, tz=_BJ)
        try:
            return bj.replace(hour=h, minute=m, second=0, microsecond=0)
        except ValueError:
            return None
    return None


def _is_once(it):
    """存储态 once 判定：repeat 为 0 / "0" / "once" / "单次" / "一次"。"""
    rep = it.get("repeat")
    if rep in (0, "0", "once", "one", "单次", "一次"):
        return True
    return False


def _plan(db, now):
    """生成迁移计划（不落盘）。返回 (new_db, report)。"""
    now_bj = datetime.fromtimestamp(now, tz=_BJ)
    report = {
        "devices": 0,
        "remapped": [],       # (did, old_id, new_id, message)
        "expired_once": [],   # (did, id, message, 目标时刻)
        "source_fixed": 0,    # 补 source 字段条数
        "untouched_local": 0,
        "already_new": 0,     # 已在新区间（<=INT_MAX 且 source=cloud）的条目
    }
    new_db = {}
    for did, items in db.items():
        if not isinstance(items, list):
            new_db[did] = items
            continue
        report["devices"] += 1
        cloud = [it for it in items if isinstance(it, dict) and _is_cloud_task(it)]
        local = [it for it in items if isinstance(it, dict) and not _is_cloud_task(it)]
        report["untouched_local"] += len(local)

        # 分类：需重映射（legacy 高位 id 或 source 缺失）/ 已在新区间 / 僵尸 once 待删
        # 僵尸判定与固件屏幕规则一致（device_control.cc GetUpcomingSchedule：
        # once 目标时刻已过 → 隐藏）：目标时刻 <= now 的 once 在设备上已不可见，
        # 且 ExpireOnceTasks 只清昨天及更早 ⇒ 镜像里留着会被每次 pull 反复重推。
        to_remap, keep, expired = [], [], []
        for it in cloud:
            iid = _task_id(it)
            if it.get("source") == SOURCE_CLOUD and iid <= CLOUD_ID_MAX:
                keep.append(it)          # 已符合新命名空间
                report["already_new"] += 1
                continue
            if _is_once(it):
                tgt = _once_target_datetime(it, now)
                if tgt is not None and tgt <= now_bj:
                    expired.append(it)
                    report["expired_once"].append(
                        (did, iid, it.get("message", ""), tgt.strftime("%Y-%m-%d %H:%M"))
                    )
                    continue
            to_remap.append(it)

        # 重映射：modified_utc 降序（新的排前面拿更小的 id），从 MIGRATE_TOP 递减
        to_remap.sort(key=_mod_utc, reverse=True)
        next_id = MIGRATE_TOP
        for it in to_remap:
            # 与 keep 中已有新 id 避让（正常不会碰撞：keep 都 <=INT_MAX 且迁移区间
            # 从 INT_MAX-100 起，若 keep 占用了该区间则继续下探）
            keep_ids = {_task_id(x) for x in keep}
            while next_id in keep_ids:
                next_id -= 1
            if next_id < CLOUD_ID_FLOOR:
                raise SystemExit(
                    f"cloud id 空间耗尽（< {CLOUD_ID_FLOOR}），device={did}；"
                    "请人工清理后重试"
                )
            old = _task_id(it)
            it["id"] = next_id
            it["source"] = SOURCE_CLOUD
            it["modified_utc"] = now
            report["remapped"].append((did, old, next_id, it.get("message", "")))
            next_id -= 1

        # keep 条目也补 modified_utc bump？不动——它们上次下行已用过当前 id，
        # bump 只会造成一次无意义重推。仅补 source 字段（不 bump）。
        for it in keep:
            if it.get("source") != SOURCE_CLOUD:
                it["source"] = SOURCE_CLOUD
                report["source_fixed"] += 1

        new_db[did] = keep + to_remap + local
    return new_db, report


def _print_report(report, apply_mode):
    mode = "APPLY" if apply_mode else "DRY-RUN"
    print(f"=== schedule_mirror id 迁移 [{mode}] ===")
    print(f"数据文件: {_PATH}")
    print(f"设备数: {report['devices']}")
    print(f"重映射: {len(report['remapped'])} 条")
    for did, old, new, msg in report["remapped"]:
        print(f"  {did}: {old} -> {new}  ({msg})")
    print(f"过期 once 待删: {len(report['expired_once'])} 条")
    for did, iid, msg, tgt in report["expired_once"]:
        print(f"  {did}: id={iid} 目标={tgt}  ({msg})")
    print(f"已在新区间: {report['already_new']} 条；补 source: {report['source_fixed']} 条")
    print(f"本地任务不动: {report['untouched_local']} 条")


def main():
    ap = argparse.ArgumentParser(description="schedule_mirror cloud id 存量迁移（2026-10-18）")
    ap.add_argument("--apply", action="store_true", help="落盘（默认 dry-run 只打印计划）")
    args = ap.parse_args()

    if not os.path.exists(_PATH):
        print(f"数据文件不存在: {_PATH}（无需迁移）")
        return 0
    db = _load()
    now = int(time.time())
    new_db, report = _plan(db, now)
    _print_report(report, args.apply)

    if not args.apply:
        print("\n[dry-run] 未写盘。确认计划无误后加 --apply 执行。")
        return 0

    # 备份（时间戳命名，绝不覆盖旧备份）
    bak = _PATH + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(_PATH, bak)
    print(f"\n备份: {bak}")
    _save(new_db)
    print("已写盘。下一步：重启容器 → 观察设备下次重连 M1/pull（新 id 下行 + legacy deletes）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
