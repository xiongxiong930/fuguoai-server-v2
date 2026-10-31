"""兼容转发：跨线程内存事件总线。

历史说明
--------
本文件原先是一份源自上游（MIT 许可）的实现。为彻底解除 MIT 派生关系，
该实现已被 **clean-room 独立重写** 版取代，代码位于
``fuguoai_core.native.event_bus``（依据行为规格书独立编写）。

本文件现在**只做显式 re-export，不含任何实现逻辑**：
- 既有的 ``from fuguoai_core.utils.event_bus import get_event_bus`` 调用方无需改动；
- ``get_event_bus()`` 转发自 native，返回**同一个**全局单例，不会出现两条总线。

兼容细节：原实现把队列上限常量命名为私有的 ``_QUEUE_MAXSIZE``，重写版命名为公开的
``QUEUE_MAXSIZE``（值同为 200）。此处两个名字都提供，保证既有测试与调用方均可导入。
"""

from fuguoai_core.native.event_bus import (
    EventBus,
    get_event_bus,
    QUEUE_MAXSIZE,
)

# 兼容原私有名（既有测试与调用方按此名导入）。
_QUEUE_MAXSIZE = QUEUE_MAXSIZE

__all__ = [
    "EventBus",
    "get_event_bus",
    "QUEUE_MAXSIZE",
    "_QUEUE_MAXSIZE",
]
