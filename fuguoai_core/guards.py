"""兼容转发：统一请求鉴权守卫。

历史说明
--------
本文件原先是一份源自上游（MIT 许可）的实现。为彻底解除 MIT 派生关系，
该实现已被 **clean-room 独立重写** 版取代，代码位于
``fuguoai_core.native.guards``（依据行为规格书独立编写）。

本文件现在**只做显式 re-export，不含任何实现逻辑**：
- 既有的 ``from fuguoai_core.guards import api_guard`` 调用方无需任何改动；
- 三者共享 native 模块内的同一份验证器状态，不会出现两套守卫状态。
"""

from fuguoai_core.native.guards import (
    set_user_verifier,
    get_current_user,
    api_guard,
)

__all__ = [
    "set_user_verifier",
    "get_current_user",
    "api_guard",
]
