"""M2 下行：设备重连拉取云端日程（契约 §5.2）。

设备发送 {type:"schedule_pull"}（重连时），后端回 {type:"schedule_push", tasks:[云端全量], deletes:[stale]}。
仅下发云端命名空间任务（source=cloud）；本地任务由设备自身为真源，不在此下发。
deletes = 设备表里有、mirror 已无的云端 id（fired/cancel 清理、迁移残留），
    由 ScheduleMirrorStore.get_stale_cloud_deletes 依据最近一次 M1 全量上行计算。
设备收到后按 id 与本地 LWW 合并（固件侧 OnIotSchedule）。

下行帧统一走 build_schedule_push：tasks 会归一成设备 JSON 契约
（字符串 repeat/date → 数字位图/date_y/m/d），与实时 push 同一条格式化路径。
"""

import json

from fuguoai_core.handle.textMessageHandler import TextMessageHandler
from fuguoai_core.handle.textMessageType import TextMessageType
from fuguoai_core.utils.schedule_downlink import build_schedule_push
from fuguoai_core.utils.util import normalize_device_id


class SchedulePullMessageHandler(TextMessageHandler):
    """处理设备端 schedule_pull 请求，回灌云端日程全量。"""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.SCHEDULE_PULL

    async def handle(self, conn, msg_json: dict) -> None:
        device_id = normalize_device_id(conn.device_id) if conn and conn.device_id else None
        if not device_id:
            return
        from fuguoai_core.utils.schedule_mirror_store import get_schedule_mirror_store
        store = get_schedule_mirror_store()
        cloud = store.get_cloud_tasks(device_id)
        # stale deletes：设备表里有、mirror 已无的云端 id（fired/cancel 清理与
        # 迁移残留）。设备没收到实时 delete 时靠这里在重连 pull 时补删。
        deletes = store.get_stale_cloud_deletes(device_id)
        payload = json.dumps(
            build_schedule_push(tasks=cloud, deletes=deletes),
            ensure_ascii=False,
        )
        try:
            await conn.safe_send(payload)
            conn.logger.bind(tag=__name__).info(
                f"[schedule-sync] device={device_id} pull 回灌云端 {len(cloud)} 条"
                f" + stale deletes {len(deletes)} 条"
            )
        except Exception as e:  # noqa: BLE001
            conn.logger.bind(tag=__name__).warning(
                f"[schedule-sync] pull 回灌失败(device={device_id}): {e}"
            )
