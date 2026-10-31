"""IoT（物联网设备）上行消息处理器。

设备侧通过 IOT 文本消息批量上报三类数据：设备描述符(descriptors)、运行状态(states)、
定时任务(schedule)。每条上报都包装成 fire-and-forget 异步任务，异常统一兜底落日志，
避免静默失败。
"""

import asyncio
from typing import Any, Callable, Dict, Tuple, TYPE_CHECKING

from fuguoai_config.logger import setup_logging

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler
from fuguoai_core.handle.textMessageHandler import TextMessageHandler
from fuguoai_core.handle.textMessageType import TextMessageType
from fuguoai_core.providers.tools.device_iot import (
    handleIotDescriptors,
    handleIotStatus,
    handleIotSchedule,
)

_LOG = setup_logging()
_TAG = __name__

# 各上报字段 → (处理函数, 参数构造器)；统一经 _run_detached 派发
_FIELD_ROUTES = {
    "descriptors": (handleIotDescriptors, lambda p: (p["descriptors"],)),
    "states": (handleIotStatus, lambda p: (p["states"],)),
    "schedule": (handleIotSchedule, lambda p: (p["schedule"], p.get("full", False))),
}


class IotTextMessageHandler(TextMessageHandler):
    """IOT 消息处理器。"""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.IOT

    async def handle(self, conn: "ConnectionHandler", packet: Dict[str, Any]) -> None:
        for field, (handler, build) in _FIELD_ROUTES.items():
            if field in packet:
                self._run_detached(conn, handler, *build(packet))

    def _run_detached(self, conn, handler: Callable, *args) -> None:
        task = asyncio.create_task(handler(conn, *args))
        self._guard_task(task)

    def _guard_task(self, task: asyncio.Task) -> None:
        """fire-and-forget 兜底：未处理异常落日志，CancelledError 忽略。"""

        def _on_resolved(done: asyncio.Task) -> None:
            try:
                done.result()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                _LOG.bind(tag=_TAG).error(f"IoT 消息处理任务异常: {exc}")

        task.add_done_callback(_on_resolved)
