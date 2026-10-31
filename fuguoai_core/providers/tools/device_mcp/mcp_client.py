"""设备侧 MCP 工具本地状态机。

一个连接对应一个实例，挂在 ``conn.mcp_client`` 上。它只负责维护
「设备暴露了哪些工具」与「进行中的调用结果」两份本地状态，真正的
收发动作由 ``mcp_handler`` 完成。字段名是契约的一部分，外部直接读取
``tools`` / ``name_mapping`` / ``call_results``，不可随意改名。
"""

import asyncio
from concurrent.futures import Future

from fuguoai_core.utils.util import sanitize_tool_name
from fuguoai_config.logger import setup_logging

logger = setup_logging()


class MCPClient:
    """跟踪单个设备侧 MCP 服务器的工具清单与异步调用结果。"""

    def __init__(self):
        # sanitized_name -> 工具描述（含 name/description/inputSchema）
        self.tools = {}
        # sanitized_name -> 设备原始工具名（调用时按此回写）
        self.name_mapping = {}
        self.ready = False
        # 调用序号 -> 结果 Future
        self.call_results = {}
        self.next_id = 1
        self.lock = asyncio.Lock()
        # get_available_tools 的缓存，工具变动时置空
        self._cached_available_tools = None

    def has_tool(self, name: str) -> bool:
        return name in self.tools

    def get_available_tools(self) -> list:
        """把本地工具清单整理成 OpenAI function-calling 格式。

        返回结构被 LLM 工具清单与 ``DeviceMCPExecutor.get_tools`` 直接消费，
        因此字典的键顺序与嵌套必须稳定。
        """
        if self._cached_available_tools is not None:
            return self._cached_available_tools

        snapshot = []
        for tool_name, tool_data in self.tools.items():
            schema = tool_data["inputSchema"]
            definition = {
                "name": tool_name,
                "description": tool_data["description"],
                "parameters": {
                    "type": schema.get("type", "object"),
                    "properties": schema.get("properties", {}),
                    "required": schema.get("required", []),
                },
            }
            snapshot.append({"type": "function", "function": definition})

        self._cached_available_tools = snapshot
        return snapshot

    async def is_ready(self) -> bool:
        async with self.lock:
            return self.ready

    async def set_ready(self, status: bool):
        async with self.lock:
            self.ready = status

    async def add_tool(self, tool_data: dict):
        async with self.lock:
            safe_name = sanitize_tool_name(tool_data["name"])
            self.tools[safe_name] = tool_data
            self.name_mapping[safe_name] = tool_data["name"]
            # 工具集合变化，下次取清单必须重新生成
            self._cached_available_tools = None

    async def get_next_id(self) -> int:
        async with self.lock:
            current = self.next_id
            self.next_id += 1
            return current

    async def register_call_result_future(self, call_id: int, future: Future):
        async with self.lock:
            self.call_results[call_id] = future

    async def resolve_call_result(self, call_id: int, result):
        async with self.lock:
            pending = self.call_results.pop(call_id, None)
            if pending is not None and not pending.done():
                pending.set_result(result)

    async def reject_call_result(self, call_id: int, exception: Exception):
        async with self.lock:
            pending = self.call_results.pop(call_id, None)
            if pending is not None and not pending.done():
                pending.set_exception(exception)

    async def cleanup_call_result(self, call_id: int):
        async with self.lock:
            self.call_results.pop(call_id, None)
