"""兼容转发：按设备隔离门禁 + 设备绑定持久层。

历史说明
--------
本文件原先是一份源自上游（MIT 许可）的实现。为彻底解除 MIT 派生关系，
该实现已被 **clean-room 独立重写** 版取代，代码位于
``fuguoai_core.native.device_access``（依据行为规格书独立编写）。

本文件现在**只做显式 re-export，不含任何实现逻辑**：
- 既有的 ``from fuguoai_core.device_access import DeviceBindingStore`` 调用方无需改动；
- 转发的是同一个类对象，所有入口读写同一张 ``device_binding`` 表。
"""

from fuguoai_core.native.device_access import (
    POLICY_ALLOW_ALL,
    POLICY_GUEST_ALLOWED,
    POLICY_REQUIRE_ACTIVATION,
    DeviceBindingStore,
    DeviceAccess,
)

__all__ = [
    "POLICY_ALLOW_ALL",
    "POLICY_GUEST_ALLOWED",
    "POLICY_REQUIRE_ACTIVATION",
    "DeviceBindingStore",
    "DeviceAccess",
]
