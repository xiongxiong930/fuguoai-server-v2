"""兼容转发：设备在线状态持久化。

历史说明
--------
本文件原先是一份源自上游（MIT 许可）的实现。为彻底解除 MIT 派生关系，
该实现已被 **clean-room 独立重写** 版取代，代码位于
``fuguoai_core.native.device_store``（依据行为规格书独立编写）。

本文件现在**只做显式 re-export，不含任何实现逻辑**：
- 既有的 ``from fuguoai_core.utils.device_store import ...`` 调用方无需改动；
- 转发的是同一批函数对象，读写同一张 ``device_binding`` 表。
"""

from fuguoai_core.native.device_store import (
    DEVICE_DB_PATH,
    init_schema,
    set_online,
    offline_stale,
    init_schema_async,
    set_online_async,
    offline_stale_async,
)

__all__ = [
    "DEVICE_DB_PATH",
    "init_schema",
    "set_online",
    "offline_stale",
    "init_schema_async",
    "set_online_async",
    "offline_stale_async",
]
