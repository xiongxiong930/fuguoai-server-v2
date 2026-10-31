"""M2 日程下行通道工具（契约 §5.2）。

- build_schedule_push：构造后端→设备下行帧 {type:"schedule_push", tasks, deletes}。
- push_schedule_to_device：经设备 8000 WS 实时下发（复用 connection_registry.send_to_device，
  不改 WS 协议格式，设备离线返回 False 由调用方忽略——离线设备靠重连 schedule_pull 补齐）。
- publish_cloud_change：向小程序 /api/v1/events 实时广播云端日程变更（event_bus，无订阅者 no-op）。

设计要点：
- 云端任务是后端真源，下行只下发云端命名空间任务（source=cloud）；本地任务由设备自身为真源，不下发。
- 云端 id 自 2026-10-18 起从 INT_MAX 递减分配（cJSON valueint 不钳位），命名空间判定只认 source。
- 增量 push 用 tasks/deletes 差分；设备重连 pull 由 handler 下发全量云端集合 + stale deletes。
"""

import json
import re
import time

from fuguoai_core.utils.util import normalize_device_id

TAG = "schedule_downlink"

# 小程序（reminders.js REPEAT_OPTIONS）与历史存储用字符串 repeat；设备
# JsonToTask 只认数字 repeat（非数字一律 fallback 0x7F=每天）→ 下行前统一
# 归一成设备位图。设备约定（device_control.cc）：bit0=周日..bit6=周六，
# 0x7F=每天、0x3E=工作日(周一~周五)、0x41=周末(周日+周六)、0=单次。
_REPEAT_STR_TO_MASK = {
    "daily": 0x7F, "everyday": 0x7F, "all": 0x7F, "每天": 0x7F,
    "once": 0, "one": 0, "单次": 0, "一次": 0,
    "weekday": 0x3E, "workday": 0x3E, "工作日": 0x3E,
    "weekend": 0x41, "周末": 0x41,
}


def _to_device_task(task):
    """存储态 cloud task → 设备 JSON 契约（JsonToTask 只认数字 repeat / date_y/m/d）。

    - repeat 字符串 daily/once/weekday/weekend → 设备数字位图；数字（含纯数字
      字符串）原样透传（语音镜像路径发的就是设备约定位图，勿二次转换）。
    - date "YYYY-MM-DD"/"YYYY/M/D" 字符串 → date_y/date_m/date_d（once 的日期
      锚点；缺省日期的 once 在设备上只能表达"当天"）。
    - source 字段**原样透传**（2026-10-18 屏显修复尝试后回退，勿再改）：
      曾尝试下行剥离 source 让设备显示 [本·通]（终端用户看到 [云·通] 困惑），
      实测不可行且有害——① 固件对 schedule_push 到达的任务统一标记 kCloud
      （8818 实证：无线 source 的载荷设备仍存 source=1），剥离达不到显示目的；
      ② HandleSchedulePush 的 deletes 只删 source==kCloud 或 id 高位的任务
      （device_control.cc:1626），新 id（INT_MAX 递减）无高位 bit，一旦设备
      存成 kLocal，fired once 的下行删除会被静默跳过 ⇒ "过时间了也没清除"
      复发。屏显 [云] 只能由固件侧改（ScheduleRowPrefix），后端不碰。
    只做格式归一、不改业务语义；无法识别的 repeat 保持原样（设备按每天处理，
    与历史行为一致，不引入新回归）。存储态不被修改（返回浅拷贝）。
    """
    if not isinstance(task, dict):
        return task
    out = dict(task)
    rep = out.get("repeat")
    if isinstance(rep, str):
        key = rep.strip().lower()
        if key in _REPEAT_STR_TO_MASK:
            out["repeat"] = _REPEAT_STR_TO_MASK[key]
        elif key.isdigit():
            out["repeat"] = int(key)
    date = out.get("date")
    if isinstance(date, str) and date.strip():
        parts = re.split(r"[-/]", date.strip())
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            y, m, d = (int(p) for p in parts)
            if 2000 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31:
                out.setdefault("date_y", y)
                out.setdefault("date_m", m)
                out.setdefault("date_d", d)
    return out


def build_schedule_push(tasks=None, deletes=None):
    """构造下行帧。tasks=云端任务列表（upsert/更新），deletes=待删除云端 id 列表。

    tasks 经 _to_device_task 归一成设备 JSON 契约（字符串 repeat/date → 数字），
    存储态（schedule_mirror.json / 小程序视图）保持原样。
    """
    return {
        "type": "schedule_push",
        "tasks": [_to_device_task(t) for t in tasks] if tasks else [],
        "deletes": list(deletes) if deletes else [],
    }


async def push_schedule_to_device(device_id, tasks=None, deletes=None) -> bool:
    """实时下发云端日程变更到设备活跃 WS。返回是否成功下发（离线/异常返回 False）。

    绝不抛异常影响调用方；设备离线时由重连 schedule_pull 自然补齐，无需报错。
    """
    did = normalize_device_id(device_id)
    if not did:
        return False
    msg = build_schedule_push(tasks, deletes)
    try:
        from fuguoai_core.utils.connection_registry import get_connection_registry
        reg = get_connection_registry()
        # send_to_device 接受 dict 并自行 JSON 序列化
        return await reg.send_to_device(did, msg)
    except Exception as e:  # noqa: BLE001
        try:
            from fuguoai_core.utils.util import logger as _log
        except Exception:
            _log = None
        if _log:
            _log.bind(tag=TAG).warning(f"下发 schedule_push 失败(device={did}): {e}")
        return False


def publish_cloud_change(device_id, action, task=None, deleted_id=None):
    """向小程序事件 WS 广播云端日程变更（create/update/delete）。无订阅者 no-op。"""
    did = normalize_device_id(device_id)
    if not did:
        return
    try:
        from fuguoai_core.utils.event_bus import get_event_bus
        get_event_bus().publish({
            "type": "schedule.cloud.changed",
            "device_id": did,
            "action": action,
            "task": task,
            "deleted_id": deleted_id,
            "ts": int(time.time()),
        })
    except Exception:  # noqa: BLE001
        pass
