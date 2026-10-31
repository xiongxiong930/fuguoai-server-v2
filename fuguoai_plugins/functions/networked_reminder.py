"""联网定时提醒工具（云端领域动作 → LLM 可调用 Function Call）。

后端真源：fuguoai_core.utils.networked_reminder_store.NetworkedReminderStore
（调度存云端，到点由服务端生成 TTS 推设备播放，固件零改动）。

本模块把"提醒"从「只能小程序 HTTP 创建」升级为「对话原生连续决策」：
  用户："提醒我明天早上 9 点吃药"  → networked_reminder_create
  用户："我设了哪些提醒"          → networked_reminder_list
  用户："取消吃药那个提醒"        → networked_reminder_cancel
  用户："先关掉每周三的提醒"      → networked_reminder_toggle

契约对齐 hass_set_state / call_device：
  - @register_function(name, desc, ToolType.SYSTEM_CTL) → conn 由执行器注入
  - 返回 ActionResponse；成功后 REQLLM 让 LLM 生成自然回复
  - 失败走 Action.ERROR + 面向用户的安全话术（不泄露内部细节）

⚠️ 启用前置：本工具须在 config[Intent][<selected>][functions] 列表中登记，
   否则只注册不下发（见 ServerPluginExecutor.get_tools 的 config 门控）。
"""

import os
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from fuguoai_config.logger import setup_logging
from fuguoai_plugins.register import (
    register_function,
    ToolType,
    ActionResponse,
    Action,
)

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

FRIENDLY_ERROR = "提醒这块刚才没能用上，等会儿再试试吧"


def _device_id(conn: "ConnectionHandler") -> str:
    """从连接取归一化设备 id（store 主键）。"""
    did = getattr(conn, "device_id", None)
    if not did:
        # 兜底：部分路径走 headers
        did = (getattr(conn, "headers", {}) or {}).get("device-id")
    return did or ""


def _uid(conn: "ConnectionHandler"):
    """设备 WS 会话无 JWT uid；尽量取，取不到传 None（store 容忍，调度按 device_id 触发）。

    已知限制：uid=None 时，小程序「我的提醒」按 uid 过滤的视图看不到对话创建的提醒，
    但提醒仍会按时推流到设备。若后续要打通小程序视图，需在此绑定 conn 上的真实 user_id。
    """
    return getattr(conn, "uid", None) or getattr(conn, "user_id", None)


# ---------------------------------------------------------------------------
# 工具描述（OpenAI function-calling 格式）
# ---------------------------------------------------------------------------

_NETWORKED_REMINDER_SCHEDULE_DESC = (
    "提醒的调度规则，必须从以下四种里选一种：\n"
    "1) 一次性(优先用相对时间，不要自己推算时间戳): "
    "{\"type\":\"once\",\"in_minutes\":30} 或 {\"type\":\"once\",\"in_seconds\":1800}\n"
    "   也支持绝对时间 {\"type\":\"once\",\"fire_at\":<北京时间Unix秒>}，但必须是未来时刻，"
    "已过去会被拒绝并返回当前时间\n"
    "2) 每天:   {\"type\":\"daily\",\"hour\":9,\"minute\":0}\n"
    "3) 每周:   {\"type\":\"weekly\",\"hour\":9,\"minute\":0,\"repeat\":[0,2,4]}  (0=周一..6=周日)\n"
    "4) 间隔:   {\"type\":\"interval\",\"every_seconds\":1800}  (每隔 N 秒，需>=900)\n"
    "时间统一用北京时间(UTC+8)的Unix秒。"
)

networked_reminder_create_desc = {
    "type": "function",
    "function": {
        "name": "networked_reminder_create",
        "description": (
            "在云端为当前设备创建一个联网定时提醒。到点后服务端会自动生成语音播报推送到设备，"
            "并同步到设备本地日程表（屏幕可见、断网也能本地触发）。\n"
            "当用户说出以下意图时调用：\n"
            "1. 设定提醒：\"提醒我明天9点吃药\"、\"每天早上8点叫我喝水\"、\"每周三下午3点开会\"、\"每30分钟提醒我休息\";\n"
            "2. 单次/周期均可，必须给出提醒内容(text)和调度规则(schedule)。\n"
            "不要用于查询或取消，那些请调对应的 list / cancel 工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "提醒内容，例如：吃药、喝水、开会。最长200字。",
                },
                "schedule": {
                    "type": "object",
                    "description": _NETWORKED_REMINDER_SCHEDULE_DESC,
                },
            },
            "required": ["text", "schedule"],
        },
    },
}

networked_reminder_list_desc = {
    "type": "function",
    "function": {
        "name": "networked_reminder_list",
        "description": (
            "列出当前设备已设置的联网定时提醒（含内容、调度、下次触发时间、状态）。\n"
            "当用户询问：\"我设了哪些提醒\"、\"有没有明天的提醒\"、\"看看我的提醒列表\" 时调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "include_disabled": {
                    "type": "boolean",
                    "description": "是否包含已关闭的提醒，默认 false（只列启用的）。",
                },
            },
            "required": [],
        },
    },
}

networked_reminder_cancel_desc = {
    "type": "function",
    "function": {
        "name": "networked_reminder_cancel",
        "description": (
            "取消(删除)当前设备的某条联网定时提醒。\n"
            "当用户说：\"取消吃药提醒\"、\"删掉那个每周三的提醒\"、\"不要提醒我了\" 时调用，"
            "需先通过 networked_reminder_list 拿到目标 rid 再传进来。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "rid": {
                    "type": "string",
                    "description": "要取消的提醒ID，来自 networked_reminder_list 返回的 rid 字段。",
                },
            },
            "required": ["rid"],
        },
    },
}

networked_reminder_toggle_desc = {
    "type": "function",
    "function": {
        "name": "networked_reminder_toggle",
        "description": (
            "开启或暂停当前设备的某条联网定时提醒（不删除）。\n"
            "当用户说：\"先关掉每周三的提醒\"、\"把吃药提醒暂停\"、\"重新开启那个提醒\" 时调用，"
            "需先通过 networked_reminder_list 拿到目标 rid。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "rid": {
                    "type": "string",
                    "description": "目标提醒ID，来自 networked_reminder_list 返回的 rid 字段。",
                },
                "enabled": {
                    "type": "boolean",
                    "description": "true=开启, false=暂停(关闭)。",
                },
            },
            "required": ["rid", "enabled"],
        },
    },
}


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------

@register_function("networked_reminder_create", networked_reminder_create_desc, ToolType.SYSTEM_CTL)
async def networked_reminder_create(conn: "ConnectionHandler", text: str, schedule: dict):
    did = _device_id(conn)
    if not did:
        return ActionResponse(action=Action.ERROR, result="missing device_id", response=FRIENDLY_ERROR)
    try:
        store = get_networked_reminder_store()
        rid, err = store.add(did, _uid(conn), text, schedule)
        if rid is None:
            logger.bind(tag=TAG).warning(f"创建联网提醒失败 device={did} err={err}")
            # 走 REQLLM 而非 ERROR：connection.py 只把 REQLLM 的 result 回喂进
            # 对话历史（ERROR 的 result 被丢弃、用户只听到固定话术），模型看不到
            # 失败原因就无法自我纠正（如 fire_at 算错年份时按返回的当前时间重算）。
            return ActionResponse(
                action=Action.REQLLM,
                result=f"创建提醒失败：{err or '参数不合法'}。请按原因修正后重试。",
                response=None,
            )
        item = store.get(did, rid) or {}
        nxt = item.get("next_fire_at")
        # B 方案（2026-10-17 语音设提醒根因修复）：daily/weekly/once 同步进设备
        # 本地表（cloud task，经 schedule_mirror_store + schedule_push），让屏幕
        # 可见且设备本地触发；interval 设备无间隔语义，仅留云端推流兜底。
        # 用 store 归一化后的 schedule（once 相对时间已转成 fire_at），勿用原始入参。
        # rid 一并写入镜像条目：提醒触发后 scheduler 按 rid 精确清理（2026-10-18）。
        await _mirror_to_device_schedule(did, text, item.get("schedule") or {}, rid=rid)
        summary = f"已创建提醒[{rid}]：{text.strip()}，下次触发={'未定' if not nxt else nxt}"
        logger.bind(tag=TAG).info(summary)
        # REQLLM：让 LLM 用自然语言确认（如「好的，已为你设置明天9点吃药的提醒」）
        return ActionResponse(action=Action.REQLLM, result=summary, response=None)
    except Exception as e:
        logger.bind(tag=TAG).exception(f"创建联网提醒异常: {e}")
        return ActionResponse(action=Action.ERROR, result=f"{type(e).__name__}: {e}", response=FRIENDLY_ERROR)


@register_function("networked_reminder_list", networked_reminder_list_desc, ToolType.SYSTEM_CTL)
async def networked_reminder_list(conn: "ConnectionHandler", include_disabled: bool = False):
    did = _device_id(conn)
    if not did:
        return ActionResponse(action=Action.ERROR, result="missing device_id", response=FRIENDLY_ERROR)
    try:
        store = get_networked_reminder_store()
        items = store.list(did, include_disabled=include_disabled)
        if not items:
            return ActionResponse(action=Action.REQLLM, result="当前设备没有已设置的联网提醒", response=None)
        # 精简下发给 LLM 的字段，避免噪声
        slim = [
            {
                "rid": it.get("rid"),
                "text": it.get("text"),
                "schedule": it.get("schedule"),
                "next_fire_at": it.get("next_fire_at"),
                "status": it.get("status"),
            }
            for it in items
        ]
        return ActionResponse(
            action=Action.REQLLM,
            result=f"共 {len(slim)} 条联网提醒：" + str(slim),
            response=None,
        )
    except Exception as e:
        logger.bind(tag=TAG).exception(f"列出联网提醒异常: {e}")
        return ActionResponse(action=Action.ERROR, result=f"{type(e).__name__}: {e}", response=FRIENDLY_ERROR)


@register_function("networked_reminder_cancel", networked_reminder_cancel_desc, ToolType.SYSTEM_CTL)
async def networked_reminder_cancel(conn: "ConnectionHandler", rid: str):
    did = _device_id(conn)
    if not did:
        return ActionResponse(action=Action.ERROR, result="missing device_id", response=FRIENDLY_ERROR)
    if not rid:
        return ActionResponse(action=Action.ERROR, result="missing rid", response="没听清要取消哪条提醒，能告诉我内容或先看看列表吗")
    try:
        store = get_networked_reminder_store()
        item = store.get(did, rid)
        if not item:
            return ActionResponse(action=Action.REQLLM, result=f"未找到提醒[{rid}]，可能已取消", response=None)
        ok = store.cancel(did, rid)
        if not ok:
            return ActionResponse(action=Action.REQLLM, result=f"未找到提醒[{rid}]，可能已取消", response=None)
        # B 方案删除环（2026-10-18 补）：cancel 只删云端 store 时会留下设备本地表
        # 里的 cloud task 僵尸（到点仍本地触发）。rid 精确匹配优先，内容匹配兜底
        # legacy 存量（迁移前无 rid 的镜像条目）。
        await _delete_mirror_task(did, item.get("text"), item.get("schedule"), rid=rid)
        logger.bind(tag=TAG).info(f"取消联网提醒 device={did} rid={rid}")
        return ActionResponse(action=Action.REQLLM, result=f"已取消提醒[{rid}]", response=None)
    except Exception as e:
        logger.bind(tag=TAG).exception(f"取消联网提醒异常: {e}")
        return ActionResponse(action=Action.ERROR, result=f"{type(e).__name__}: {e}", response=FRIENDLY_ERROR)


@register_function("networked_reminder_toggle", networked_reminder_toggle_desc, ToolType.SYSTEM_CTL)
async def networked_reminder_toggle(conn: "ConnectionHandler", rid: str, enabled: bool):
    did = _device_id(conn)
    if not did:
        return ActionResponse(action=Action.ERROR, result="missing device_id", response=FRIENDLY_ERROR)
    if not rid:
        return ActionResponse(action=Action.ERROR, result="missing rid", response="没听清要操作哪条提醒，能告诉我内容或先看看列表吗")
    try:
        store = get_networked_reminder_store()
        ok = store.set_enabled(did, rid, bool(enabled))
        if not ok:
            return ActionResponse(action=Action.REQLLM, result=f"未找到提醒[{rid}]", response=None)
        verb = "开启" if enabled else "暂停"
        logger.bind(tag=TAG).info(f"{verb}联网提醒 device={did} rid={rid}")
        return ActionResponse(action=Action.REQLLM, result=f"已{verb}提醒[{rid}]", response=None)
    except Exception as e:
        logger.bind(tag=TAG).exception(f"切换联网提醒异常: {e}")
        return ActionResponse(action=Action.ERROR, result=f"{type(e).__name__}: {e}", response=FRIENDLY_ERROR)


# 延迟导入，避免模块顶层硬依赖 fuguoai_core（auto_import 时 fuguoai_core 已可用，但保持解耦）
def get_networked_reminder_store():
    from fuguoai_core.utils.networked_reminder_store import get_networked_reminder_store as _g
    return _g()


# ---------------------------------------------------------------------------
# B 方案：云端提醒 → 设备本地表同步（2026-10-17 语音设提醒根因修复）
# ---------------------------------------------------------------------------

def _weekly_repeat_bitmask(repeat_list):
    """weekly repeat 数组(0=周一..6=周日，与 store _compute_next 的 Python
    weekday() 一致) → 设备位图。

    设备端约定（device_control.cc ParseRepeatMask / ComputeNextFire）：
    bit0=周日 .. bit6=周六（0x3E=工作日=周一~周五、0x41=周末、0x7F=每天）。
    2026-10-18 修正：旧实现直接 1<<d（bit0=周一），与设备位图语义**相反**，
    语音设"每周三"会变成周二响。现按 (d+1)%7 转换：周一(0)→bit1、周日(6)→bit0。
    空/越界返回 0（调用方拒收）。
    """
    mask = 0
    for d in (repeat_list or []):
        if isinstance(d, int) and 0 <= d <= 6:
            mask |= (1 << ((d + 1) % 7))
    return mask


def _once_fields(fire_at, text):
    """once 的 fire_at(北京时间unix秒) → 设备 cloud task 字段。

    设备 JSON 契约（device_control.cc JsonToTask / GetUpcomingSchedule）：
    repeat=0 表示单次；目标日期走 date_y/date_m/date_d 数字字段（缺省=当天，
    无日期锚点的旧 once 会被 LoadFromNvs 当过期清理）。日期按北京墙钟取；
    目标日期<今天的任务设备直接隐藏，故调用方须保证 fire_at 是未来时刻。
    """
    bj = datetime.fromtimestamp(int(fire_at), tz=timezone(timedelta(hours=8)))
    return {
        "hour": bj.hour,
        "minute": bj.minute,
        "repeat": 0,
        "date_y": bj.year,
        "date_m": bj.month,
        "date_d": bj.day,
        "message": (text or "").strip(),
    }


async def _mirror_to_device_schedule(device_id, text, schedule, rid=None):
    """把 daily/weekly/once 云端提醒同步成设备 cloud task，经 schedule_push 下发设备本地表。

    - daily  → {hour, minute, repeat:127, message:text}
    - weekly → {hour, minute, repeat:位图(bit0=周日), message:text}
    - once   → {hour, minute, repeat:0, date_y/date_m/date_d(北京), message:text}
    - interval / 未知 → 返回 None（设备无间隔语义，仅留云端推流兜底）

    2026-10-18 修正：旧版 once 被显式排除（注释称"SetTimerTask 无 one-shot
    语义"——该判断错误，固件 device_control.cc 明确支持 repeat=once + date），
    导致语音设的单次提醒在设备日程屏上永远不可见。

    rid（2026-10-18 新增）：写入镜像条目，提醒触发后 scheduler 按 rid 精确
    删除镜像 + 下行 deletes，避免 once 响完后在设备表残留（用户实测反馈
    "过时间了也没清除"）。存量无 rid 条目靠 pull stale-deletes 差集清理。

    纯 best-effort：屏幕同步失败不影响云端提醒（系统1 到点仍推流）；在线/离线由
    push_schedule_to_device 自行判定，离线设备靠重连 schedule_pull 补齐。
    """
    if not isinstance(schedule, dict):
        return None
    typ = schedule.get("type")
    if typ == "daily":
        h, m = schedule.get("hour"), schedule.get("minute")
        if not isinstance(h, int) or not isinstance(m, int):
            return None
        fields = {"hour": h, "minute": m, "repeat": 127, "message": (text or "").strip()}
    elif typ == "weekly":
        h, m = schedule.get("hour"), schedule.get("minute")
        if not isinstance(h, int) or not isinstance(m, int):
            return None
        mask = _weekly_repeat_bitmask(schedule.get("repeat"))
        if mask == 0:
            return None
        fields = {"hour": h, "minute": m, "repeat": mask, "message": (text or "").strip()}
    elif typ == "once":
        fa = schedule.get("fire_at")
        if not isinstance(fa, (int, float)) or fa <= 0:
            return None
        if int(fa) <= int(time.time()):
            # 已过去：设备必隐藏（目标日期<今天），且云端会立即推流，镜像无意义
            logger.bind(tag=TAG).info(
                f"once 提醒已过去，跳过设备镜像 device={device_id} fire_at={int(fa)}"
            )
            return None
        fields = _once_fields(fa, text)
    else:
        return None  # interval / 未知类型仅云端推流
    if rid:
        fields["rid"] = rid
    try:
        from fuguoai_core.utils.schedule_mirror_store import get_schedule_mirror_store
        from fuguoai_core.utils.schedule_downlink import push_schedule_to_device, publish_cloud_change
        task = get_schedule_mirror_store().create_cloud_task(device_id, fields)
        await push_schedule_to_device(device_id, tasks=[task], deletes=[])
        publish_cloud_change(device_id, "create", task=task)
        logger.bind(tag=TAG).info(
            f"语音提醒同步设备日程 device={device_id} task_id={task.get('id')} type={typ}"
        )
    except Exception as e:  # noqa: BLE001
        logger.bind(tag=TAG).warning(f"语音提醒同步设备日程失败 device={device_id}: {e}")


def _match_fields(text, schedule):
    """提醒内容 → 设备镜像 task 的匹配字段（取消时定位已镜像任务用）。

    字段与 _mirror_to_device_schedule 构造的保持同构；无法匹配的类型
    （interval / 字段缺失）返回 None（未镜像过，无需清理）。
    """
    if not isinstance(schedule, dict):
        return None
    typ = schedule.get("type")
    msg = (text or "").strip()
    if typ == "daily":
        h, m = schedule.get("hour"), schedule.get("minute")
        if not isinstance(h, int) or not isinstance(m, int):
            return None
        return {"hour": h, "minute": m, "repeat": 127, "message": msg}
    if typ == "weekly":
        h, m = schedule.get("hour"), schedule.get("minute")
        if not isinstance(h, int) or not isinstance(m, int):
            return None
        mask = _weekly_repeat_bitmask(schedule.get("repeat"))
        if mask == 0:
            return None
        return {"hour": h, "minute": m, "repeat": mask, "message": msg}
    if typ == "once":
        fa = schedule.get("fire_at")
        if not isinstance(fa, (int, float)) or fa <= 0:
            return None
        f = _once_fields(fa, text)
        return {
            "hour": f["hour"], "minute": f["minute"], "repeat": 0,
            "date_y": f["date_y"], "date_m": f["date_m"], "date_d": f["date_d"],
            "message": msg,
        }
    return None


async def _delete_mirror_task(device_id, text, schedule, rid=None):
    """取消提醒时，把已镜像到设备日程表的 cloud task 一并删除（best-effort）。

    两条删除路径：
    1. rid 精确匹配（2026-10-18 起镜像条目带 rid）：delete_cloud_task_by_rid
       一步命中，不受同内容多条提醒干扰；
    2. 内容匹配兜底（hour/minute/repeat/date_y/m/d/message）：覆盖迁移前
       无 rid 的 legacy 存量条目。
    delete_cloud_task 只删云端命名空间，不碰设备本地任务；删除经 schedule_push
    的 deletes 通道下行，离线设备重连 pull 时后端已无该任务，自然不再下发。
    """
    try:
        from fuguoai_core.utils.schedule_mirror_store import get_schedule_mirror_store
        from fuguoai_core.utils.schedule_downlink import push_schedule_to_device, publish_cloud_change
        store = get_schedule_mirror_store()
        # 路径 1：rid 精确删除（命中等价于内容匹配成功，直接返回）
        if rid:
            iid = store.delete_cloud_task_by_rid(device_id, rid)
            if iid is not None:
                await push_schedule_to_device(device_id, tasks=[], deletes=[iid])
                publish_cloud_change(device_id, "delete", deleted_id=iid)
                logger.bind(tag=TAG).info(
                    f"取消提醒同步删除设备日程(rid) device={device_id} task_id={iid}"
                )
                return
        # 路径 2：内容匹配兜底（legacy 无 rid 存量）
        fields = _match_fields(text, schedule)
        if fields is None:
            return
        for t in store.get_cloud_tasks(device_id):
            if all(t.get(k) == v for k, v in fields.items()):
                if store.delete_cloud_task(device_id, t["id"]):
                    await push_schedule_to_device(device_id, tasks=[], deletes=[t["id"]])
                    publish_cloud_change(device_id, "delete", deleted_id=t["id"])
                    logger.bind(tag=TAG).info(
                        f"取消提醒同步删除设备日程 device={device_id} task_id={t.get('id')}"
                    )
                return
    except Exception as e:  # noqa: BLE001
        logger.bind(tag=TAG).warning(f"取消提醒删除设备日程失败 device={device_id}: {e}")

