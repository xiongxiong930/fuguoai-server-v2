"""MCP 接入点客户端（复果自有实现）。

维护与远端接入点之间的一条长连接状态，并对外暴露：

* 工具仓库 —— 接入点推送的工具按清洗名缓存，并保留清洗名到原始名的映射；
* 调用结果回填 —— 每次调用分配自增 id，结果经 Future 异步回填；
* 发送串行化 —— 收发复用一把发送锁，避免并发帧交错。

所有状态变更都集中在同一把状态锁内完成，保证多连接并发时的可见性。
"""

import asyncio
from concurrent.futures import Future

from fuguoai_config.logger import setup_logging
from fuguoai_core.utils.util import sanitize_tool_name

TAG = __name__
logger = setup_logging()


class MCPEndpointClient:
    """接入点客户端：管理连接状态与工具清单。"""

    def __init__(self, conn=None):
        self.conn = conn
        # 清洗名 -> 工具元数据
        self.tools = {}
        # 清洗名 -> 原始名
        self.name_mapping = {}
        self.ready = False
        # 调用 id -> 用于回填的 Future
        self.call_results = {}
        self.next_id = 1
        self.lock = asyncio.Lock()
        # 发送串行锁：websockets 14.2 的 send 无内部写锁，需自行串行化
        self._send_lock = asyncio.Lock()
        # 工具清单缓存（OpenAI function-calling 形状）
        self._cached_available_tools = None
        self.websocket = None

    # ---- 连接态 ----

    def has_tool(self, name: str) -> bool:
        """接入点是否已暴露名为 ``name`` 的工具。"""
        return name in self.tools

    def set_websocket(self, websocket):
        """绑定底层 WebSocket 句柄。"""
        self.websocket = websocket

    @property
    def is_connected(self) -> bool:
        """连接是否已建立（可观测性）。"""
        return self.websocket is not None

    async def send_message(self, message: str):
        """把一条 JSON-RPC 报文发往接入点。"""
        if self.websocket is None:
            raise RuntimeError("WebSocket连接未建立")
        async with self._send_lock:
            await self.websocket.send(message)

    async def close(self):
        """关闭 WebSocket（异常安全 + 幂等）。

        先取句柄置空再关闭：若 close 抛异常（半开连接）会被吞掉并告警，
        重复调用则直接 no-op，不会二次 close 报错。
        """
        handle = self.websocket
        if handle is None:
            return
        self.websocket = None
        try:
            await handle.close()
        except Exception as exc:
            logger.bind(tag=TAG).warning(f"MCP接入点连接关闭异常: {exc}")

    # ---- 就绪标志 ----

    async def is_ready(self) -> bool:
        async with self.lock:
            return self.ready

    async def set_ready(self, status: bool):
        async with self.lock:
            self.ready = status

    # ---- 工具仓库 ----

    async def add_tool(self, tool_data: dict):
        """登记一个接入点推送的工具，并作废清单缓存。"""
        async with self.lock:
            clean_name = sanitize_tool_name(tool_data["name"])
            self.tools[clean_name] = tool_data
            self.name_mapping[clean_name] = tool_data["name"]
            self._cached_available_tools = None

    def get_available_tools(self) -> list:
        """返回 OpenAI function-calling 形状的工具清单（带缓存）。"""
        if self._cached_available_tools is not None:
            return self._cached_available_tools

        compiled = []
        for clean_name, meta in self.tools.items():
            schema = meta["inputSchema"]
            compiled.append(
                {
                    "type": "function",
                    "function": {
                        "name": clean_name,
                        "description": meta["description"],
                        "parameters": {
                            "type": schema.get("type", "object"),
                            "properties": schema.get("properties", {}),
                            "required": schema.get("required", []),
                        },
                    },
                }
            )

        self._cached_available_tools = compiled
        return compiled

    # ---- 调用结果回填 ----

    async def get_next_id(self) -> int:
        """分配下一个自增调用 id。"""
        async with self.lock:
            assigned = self.next_id
            self.next_id += 1
            return assigned

    async def register_call_result_future(self, call_id: int, future: Future):
        """登记某个调用 id 对应的结果 Future。"""
        async with self.lock:
            self.call_results[call_id] = future

    async def resolve_call_result(self, call_id: int, result: any):
        """以正常结果回填指定调用 id。"""
        future = await self._pop_future(call_id)
        if future is not None and not future.done():
            future.set_result(result)

    async def reject_call_result(self, call_id: int, exception: Exception):
        """以异常回填指定调用 id。"""
        future = await self._pop_future(call_id)
        if future is not None and not future.done():
            future.set_exception(exception)

    async def cleanup_call_result(self, call_id: int):
        """丢弃指定调用 id 的 Future（不设值也不设异常）。"""
        await self._pop_future(call_id)

    async def _pop_future(self, call_id: int):
        """取出并移除一个挂起的 Future（线程安全）。"""
        async with self.lock:
            return self.call_results.pop(call_id, None)
