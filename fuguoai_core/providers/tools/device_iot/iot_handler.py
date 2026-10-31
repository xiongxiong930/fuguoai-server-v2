"""设备上报的 IoT 描述符 / 状态 / 日程 接入处理（复果自有实现）。

设备首次连接或能力变化时，会上报它的「物模型」(descriptors)、实时状态(states)与
本地日程(schedule)。本模块负责把这些不可信的上行数据桥接到连接对象：建立
``conn.iot_descriptors``、把工具注册进统一工具处理器、按类型把状态写回属性值、
并把日程合并进镜像存储（绝不反向写回设备）。
"""
import asyncio
from typing import TYPE_CHECKING

from fuguoai_config.logger import setup_logging
from .iot_descriptor import IotDescriptor
from fuguoai_core.utils.util import normalize_device_id

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

# func_handler 就绪等待上限（秒）
_INIT_WAIT = 5


async def handleIotDescriptors(conn: "ConnectionHandler", descriptors):
    """处理设备上报的物模型描述（descriptors 来自设备，不可信）。"""
    if not isinstance(descriptors, list):
        logger.bind(tag=TAG).warning(
            f"descriptors 非 list(类型={type(descriptors).__name__})，跳过")
        return

    waited = 0
    while (not hasattr(conn, "func_handler") or conn.func_handler is None
           or not conn.func_handler.finish_init):
        await asyncio.sleep(1)
        waited += 1
        if waited >= _INIT_WAIT:
            logger.bind(tag=TAG).debug("连接对象没有func_handler")
            return

    changed = False
    for raw in descriptors:
        if not isinstance(raw, dict):
            logger.bind(tag=TAG).warning(
                f"descriptor 非 dict(类型={type(raw).__name__})，跳过")
            continue
        if "properties" not in raw and "methods" not in raw:
            continue

        # 缺 properties 时，从 methods 参数反推一份（保证工具可注册）
        if "properties" not in raw:
            raw["properties"] = {}
            methods = raw.get("methods")
            if isinstance(methods, dict):
                for mname, minfo in methods.items():
                    if not isinstance(minfo, dict):
                        continue
                    params = minfo.get("parameters")
                    if isinstance(params, dict):
                        for pname, pinfo in params.items():
                            if not isinstance(pinfo, dict):
                                continue
                            raw["properties"][pname] = {
                                "description": pinfo.get("description", ""),
                                "type": pinfo.get("type", "string"),
                            }

        name = raw.get("name")
        if not name:
            logger.bind(tag=TAG).warning("descriptor 缺 name，跳过")
            continue

        conn.iot_descriptors[name] = IotDescriptor(
            name, raw.get("description", ""), raw.get("properties"), raw.get("methods"))
        changed = True

    if changed and hasattr(conn, "func_handler"):
        # register_iot_tools 内部会再校验畸形条目
        await conn.func_handler.register_iot_tools(descriptors)
        conn.func_handler.current_support_functions()


async def handleIotStatus(conn: "ConnectionHandler", states):
    """处理设备上报的实时状态（states 来自设备，不可信）。"""
    if not isinstance(states, list):
        logger.bind(tag=TAG).warning(
            f"states 非 list(类型={type(states).__name__})，跳过")
        return
    for entry in states:
        if not isinstance(entry, dict):
            continue
        sname = entry.get("name")
        body = entry.get("state")
        if not sname or not isinstance(body, dict):
            continue
        for key, desc in conn.iot_descriptors.items():
            if key != sname:
                continue
            for prop in desc.properties:
                pname = prop.get("name")
                if pname is None:
                    continue
                if pname not in body:
                    continue
                incoming = body[pname]
                if type(incoming) != type(prop.get("value")):
                    logger.bind(tag=TAG).error(f"属性{pname}的值类型不匹配")
                    break
                prop["value"] = incoming
                logger.bind(tag=TAG).info(f"物联网状态更新: {key} , {pname} = {incoming}")
            break
        break


async def handleIotSchedule(conn: "ConnectionHandler", schedule, full):
    """处理设备上行日程（契约 Part A.7 / M2 §5.1）。

    两种上行形态按结构分派（兼容 M1→M2 过渡，零回归）：
    - M2 增量对象：``{"op": "upsert"|"delete", "tasks":[...]}``，按 id 做 LWW merge / tombstone
    - M1 全量数组：按「M1 全量上行」安全合并——替换本地子集、保留云端任务
    后端只镜像 / 合并，绝不反向写回设备。
    """
    device_id = normalize_device_id(conn.device_id) if conn and conn.device_id else None
    if not device_id:
        logger.bind(tag=TAG).warning("[schedule-mirror] 丢弃上行: device_id 缺失")
        return
    from fuguoai_core.utils.schedule_mirror_store import get_schedule_mirror_store

    store = get_schedule_mirror_store()

    if isinstance(schedule, dict) and "op" in schedule:
        op = schedule.get("op")
        tasks = schedule.get("tasks") or []
        if not isinstance(tasks, list):
            tasks = []
        n_up, n_del = store.merge_ops(device_id, op, tasks)
        logger.bind(tag=TAG).info(
            f"[schedule-mirror] device={device_id} M2 op={op} "
            f"upsert={n_up} delete={n_del}")
        return

    if isinstance(schedule, list):
        n_local, n_cloud = store.merge_full_sync(device_id, schedule)
        # 原样留存设备全表（含 cloud 项）：pull 回灌的 stale deletes 差集依据
        store.save_device_table(device_id, schedule)
        logger.bind(tag=TAG).info(
            f"[schedule-mirror] device={device_id} M1 全量合并 "
            f"local={n_local} cloud保留={n_cloud} (full={full})")
        return

    logger.bind(tag=TAG).warning(
        f"[schedule-mirror] 丢弃上行: device_id={device_id} "
        f"schedule类型={type(schedule).__name__}")
