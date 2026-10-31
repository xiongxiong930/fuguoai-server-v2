"""工具执行器的统一抽象（复果自有实现，clean-room 重写）。

所有具体执行器（服务端插件、各类 MCP、设备 IoT 等）都实现该抽象基类，
由 :class:`ToolManager` 统一调度与路由。
"""

from abc import ABC, abstractmethod
from typing import Any, Dict

from fuguoai_plugins.register import ActionResponse

from .tool_types import ToolDefinition


class ToolExecutor(ABC):
    """执行器契约：托管一组工具，并负责这些工具的具体执行。"""

    @abstractmethod
    async def execute(
        self, conn, tool_name: str, arguments: Dict[str, Any]
    ) -> ActionResponse:
        """执行名为 ``tool_name`` 的工具，``arguments`` 为入参，返回标准化结果。"""
        raise NotImplementedError

    @abstractmethod
    def get_tools(self) -> Dict[str, ToolDefinition]:
        """列出本执行器当前托管的全部工具，键为工具名。"""
        raise NotImplementedError

    @abstractmethod
    def has_tool(self, tool_name: str) -> bool:
        """判断 ``tool_name`` 是否由本执行器托管。"""
        raise NotImplementedError
