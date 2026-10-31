"""服务端 MCP 工具执行器（复果自有实现）。

把连接维度的服务端 MCP 管理器封装成对话链路统一调度的标准执行器：
* 首次使用时惰性拉起背后的 MCP 管理器；
* 把上层下发的 ``mcp_`` 前缀工具名还原为 MCP 侧原名；
* 把 MCP 调用结果归一为 ``ActionResponse`` 交还 LLM 继续对话。
"""
from typing import Any, Dict, Optional

from ..base import ToolDefinition as _ToolDef, ToolExecutor as _Executor, ToolType as _ToolType
from fuguoai_plugins.register import Action as _Act, ActionResponse as _Resp
from .mcp_manager import ServerMCPManager

_PREFIX = "mcp_"


class ServerMCPExecutor(_Executor):
    """对话链路使用的服务端 MCP 执行器。"""

    def __init__(self, conn) -> None:
        self.conn = conn
        self._agent: Optional[ServerMCPManager] = None
        self._booted = False

    async def initialize(self) -> None:
        if self._booted:
            return
        self._agent = ServerMCPManager(self.conn)
        self._booted = True
        await self._agent.initialize_servers()

    @staticmethod
    def _strip_prefix(name: str) -> str:
        return name[len(_PREFIX):] if name.startswith(_PREFIX) else name

    async def execute(
        self, conn, tool_name: str, arguments: Dict[str, Any]
    ) -> _Resp:
        if not self._booted or not self._agent:
            return _Resp(action=_Act.ERROR, response="MCP管理器未初始化")
        bare = self._strip_prefix(tool_name)
        try:
            payload = await self._agent.execute_tool(bare, arguments)
        except Exception as err:
            if isinstance(err, ValueError):
                return _Resp(action=_Act.NOTFOUND, response=str(err))
            return _Resp(action=_Act.ERROR, response=str(err))
        return _Resp(action=_Act.REQLLM, result=str(payload))

    def get_tools(self) -> Dict[str, _ToolDef]:
        if not self._booted or not self._agent:
            return {}
        catalog: Dict[str, _ToolDef] = {}
        for item in self._agent.get_all_tools():
            meta = item.get("function", {})
            key = meta.get("name", "")
            if not key:
                continue
            catalog[key] = _ToolDef(
                name=key, description=item, tool_type=_ToolType.SERVER_MCP
            )
        return catalog

    def has_tool(self, tool_name: str) -> bool:
        if not self._booted or not self._agent:
            return False
        return self._agent.is_mcp_tool(self._strip_prefix(tool_name))

    async def cleanup(self) -> None:
        if self._agent:
            await self._agent.cleanup_all()
