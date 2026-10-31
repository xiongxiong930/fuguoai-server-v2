"""兼容转发：全局共享配置单例。

历史说明
--------
本文件原先是一份源自上游（MIT 许可）的实现。为彻底解除 MIT 派生关系，
该实现已被 **clean-room 独立重写** 版取代，代码位于
``fuguoai_core.native.config_holder``（依据行为规格书独立编写，未沿用原实现）。

本文件现在**只做显式 re-export，不含任何实现逻辑**：
- 既有的 ``from fuguoai_core.utils.config_holder import ...`` 调用方无需任何改动；
- 由于转发的是同一批函数对象，所有入口共享**同一个**配置 dict 单例，
  热加载（reload）对全部持有者同时生效，不会出现两份配置。
"""

from fuguoai_core.native.config_holder import (
    set_config,
    get_config,
    reload,
    get_last_reload,
    start_watchdog,
)

__all__ = [
    "set_config",
    "get_config",
    "reload",
    "get_last_reload",
    "start_watchdog",
]
