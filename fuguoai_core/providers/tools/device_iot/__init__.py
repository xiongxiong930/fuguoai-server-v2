"""设备端 IoT 工具模块：把设备能力（描述符 / 状态 / 日程）桥接为可执行工具。"""

from .iot_descriptor import IotDescriptor
from .iot_handler import handleIotDescriptors, handleIotStatus, handleIotSchedule
from .iot_executor import DeviceIoTExecutor

__all__ = [
    "IotDescriptor",
    "handleIotDescriptors",
    "handleIotStatus",
    "DeviceIoTExecutor",
]
