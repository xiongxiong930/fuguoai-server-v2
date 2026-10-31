"""服务端 MCP 客户端（复果自有实现）。

按配置拉起单个 MCP 服务（stdio / SSE / Streamable HTTP 三种传输），并把
它暴露成可供对话侧调用的工具。每个连接持有独立实例，断开时统一回收。

为贴近生产稳定性，本模块固化了若干行为钉子（重写时不可破坏）：
* ``call_tool`` 给同线程 / 跨线程调用都加超时边界，底层会话调用挂死时
  不会无限冻结调用方；
* 工作协程异常时用 ``logger.exception`` 记录完整 traceback，并在失败时
  置位就绪事件，避免初始化无限挂起；
* ``cleanup`` 把冗余的异常分支化简为单一 ``Exception``，关闭失败被吞掉且
  不向上传播；
* 工作协程意外终止时通过 ``add_done_callback`` 记录根因。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import shutil
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any, Dict, List, Optional

from mcp import ClientSession, Implementation, StdioServerParameters
from mcp.client.session import (
    ElicitationFnT,
    ListRootsFnT,
    LoggingFnT,
    MessageHandlerFnT,
    SamplingFnT,
)
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.session import ProgressFnT
from mcp.types import LoggingMessageNotificationParams

from fuguoai_config.logger import setup_logging
from fuguoai_core.utils.util import sanitize_tool_name

_TAG = __name__


class ServerMCPClient:
    """连接并管理单个 MCP 服务，对外提供工具清单与调用通道。"""

    def __init__(self, config: Dict[str, Any]) -> None:
        self.logger = setup_logging()
        self.config = config

        self._worker_task: Optional[asyncio.Task] = None
        self._ready_evt = asyncio.Event()
        self._halt = asyncio.Event()

        self.session: Optional[ClientSession] = None
        self._raw_tools: List = []  # 原始工具对象
        self.tools_dict: Dict[str, Any] = {}
        self._orig_name: Dict[str, str] = {}

    # ---- 生命周期 ----

    async def initialize(
        self,
        read_timeout_seconds: timedelta | None = None,
        sampling_callback: SamplingFnT | None = None,
        elicitation_callback: ElicitationFnT | None = None,
        list_roots_callback: ListRootsFnT | None = None,
        logging_callback: LoggingFnT | None = None,
        message_handler: MessageHandlerFnT | None = None,
        client_info: Implementation | None = None,
    ) -> None:
        """拉起后台工作协程并等待其就绪。"""
        if self._worker_task:
            return

        self._worker_task = asyncio.create_task(
            self._worker(
                read_timeout_seconds=read_timeout_seconds,
                sampling_callback=sampling_callback,
                elicitation_callback=elicitation_callback,
                list_roots_callback=list_roots_callback,
                logging_callback=logging_callback,
                message_handler=message_handler,
                client_info=client_info,
            ),
            name="ServerMCPClientWorker",
        )
        # 后台工作协程意外终止时记录根因，避免静默丢失诊断信息。
        self._worker_task.add_done_callback(self._note_worker_failure)
        await self._ready_evt.wait()

        self.logger.bind(tag=_TAG).info(
            f"服务端MCP客户端握手完成，可用工具: "
            f"{[name for name in self.tools_dict.values()]}"
        )

    async def cleanup(self) -> None:
        """回收客户端资源。"""
        if not self._worker_task:
            return

        self._halt.set()
        try:
            await asyncio.wait_for(self._worker_task, timeout=20)
        except Exception as exc:  # 单个客户端关闭失败被吞掉，不向上传播
            self.logger.bind(tag=_TAG).error(f"回收服务端MCP客户端时出错: {exc}")
        finally:
            self._worker_task = None

    def has_tool(self, name: str) -> bool:
        """判断当前服务是否提供名为 ``name`` 的工具。"""
        return name in self.tools_dict

    def _note_worker_failure(self, task: asyncio.Task) -> None:
        """工作协程结束回调：异常终止时记录根因。"""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            self.logger.bind(tag=_TAG).error(
                f"服务端MCP客户端后台协程异常退出: {exc}"
            )

    # ---- 工具查询 ----

    def get_available_tools(self) -> List[Dict[str, Any]]:
        """返回所有可用工具的函数式定义（供 LLM 工具清单使用）。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.description,
                    "parameters": tool.inputSchema,
                },
            }
            for name, tool in self.tools_dict.items()
        ]

    # ---- 工具调用 ----

    async def call_tool(
        self,
        name: str,
        arguments: dict,
        read_timeout_seconds: timedelta | None = None,
        progress_callback: ProgressFnT | None = None,
        *,
        meta: dict[str, Any] | None = None,
        call_timeout: float = 30.0,
    ) -> Any:
        """调用指定工具，调用全程带超时边界。"""
        if not self.session:
            raise RuntimeError("服务端MCP客户端未初始化")

        real_name = self._orig_name.get(name, name)
        loop = self._worker_task.get_loop()
        pending = self.session.call_tool(
            real_name,
            arguments=arguments,
            read_timeout_seconds=read_timeout_seconds,
            progress_callback=progress_callback,
            meta=meta,
        )

        # 同线程也加超时，避免底层会话调用挂死调用方；跨线程调用同理。
        if loop is asyncio.get_running_loop():
            return await asyncio.wait_for(pending, timeout=call_timeout)

        bridge: concurrent.futures.Future = asyncio.run_coroutine_threadsafe(
            pending, loop
        )
        return await asyncio.wait_for(
            asyncio.wrap_future(bridge), timeout=call_timeout
        )

    def is_connected(self) -> bool:
        """检查客户端是否处于正常连接状态。"""
        if self._worker_task is None or self._worker_task.done():
            return False
        if self.session is None:
            return False
        return True

    # ---- 后台工作协程 ----

    async def _worker(
        self,
        read_timeout_seconds: timedelta | None = None,
        sampling_callback: SamplingFnT | None = None,
        elicitation_callback: ElicitationFnT | None = None,
        list_roots_callback: ListRootsFnT | None = None,
        logging_callback: LoggingFnT | None = None,
        message_handler: MessageHandlerFnT | None = None,
        client_info: Implementation | None = None,
    ) -> None:
        """建立传输通道、握手并拉取工具清单，随后挂起等待关闭信号。"""
        async with AsyncExitStack() as stack:
            try:
                read_stream, write_stream = await self._open(stack)

                self.session = await stack.enter_async_context(
                    ClientSession(
                        read_stream=read_stream,
                        write_stream=write_stream,
                        read_timeout_seconds=read_timeout_seconds,
                        sampling_callback=sampling_callback,
                        elicitation_callback=elicitation_callback,
                        list_roots_callback=list_roots_callback,
                        logging_callback=logging_callback,
                        message_handler=message_handler,
                        client_info=client_info,
                    )
                )
                await self.session.initialize()

                # 拉取工具并把名字规整后登记到查表结构
                self._raw_tools = (await self.session.list_tools()).tools
                for tool in self._raw_tools:
                    sanitized = sanitize_tool_name(tool.name)
                    self.tools_dict[sanitized] = tool
                    self._orig_name[sanitized] = tool.name

                self._ready_evt.set()

                # 挂起等待关闭
                await self._halt.wait()

            except Exception as exc:
                # 异常时记录完整 traceback，便于诊断工作协程崩溃根因
                self.logger.bind(tag=_TAG).exception(
                    f"服务端MCP客户端后台协程报错: {exc}"
                )
                self._ready_evt.set()
                raise

    async def _open(self, stack: AsyncExitStack):
        """根据配置建立 stdio / SSE / Streamable HTTP 三种传输之一。"""
        if "command" in self.config:
            return await self._open_local(stack)
        if "url" in self.config:
            return await self._open_remote(stack)
        raise ValueError("MCP客户端配置必须包含'command'或'url'")

    async def _open_local(self, stack: AsyncExitStack):
        """本地子进程传输：把命令、参数与环境变量交给 stdio 客户端。"""
        command = self.config["command"]
        resolved = shutil.which("npx") if command == "npx" else command
        env = {**os.environ, **self.config.get("env", {})}
        params = StdioServerParameters(
            command=resolved,
            args=self.config.get("args", []),
            env=env,
        )
        stdio_r, stdio_w = await stack.enter_async_context(stdio_client(params))
        return stdio_r, stdio_w

    async def _open_remote(self, stack: AsyncExitStack):
        """远程传输：SSE 或 Streamable HTTP，按 ``transport`` 配置选择。"""
        headers = dict(self.config.get("headers", {}))
        # 兼容旧配置：把顶层 API_ACCESS_TOKEN 合并进 Authorization 头
        legacy_token = self.config.get("API_ACCESS_TOKEN")
        if legacy_token is not None:
            headers["Authorization"] = f"Bearer {legacy_token}"
            self.logger.bind(tag=_TAG).warning(
                "你正在使用旧过时的配置 API_ACCESS_TOKEN ，请在"
                ".mcp_server_settings.json中将API_ACCESS_TOKEN直接设置在"
                "headers中，例如 'Authorization': 'Bearer API_ACCESS_TOKEN'"
            )

        transport_type = self.config.get("transport", "sse")

        if transport_type in ("streamable-http", "http"):
            http_r, http_w, _ = await stack.enter_async_context(
                streamablehttp_client(
                    url=self.config["url"],
                    headers=headers,
                    timeout=self.config.get("timeout", 30),
                    sse_read_timeout=self.config.get("sse_read_timeout", 60 * 5),
                    terminate_on_close=self.config.get("terminate_on_close", True),
                )
            )
            return http_r, http_w

        sse_r, sse_w = await stack.enter_async_context(
            sse_client(
                url=self.config["url"],
                headers=headers,
                timeout=self.config.get("timeout", 5),
                sse_read_timeout=self.config.get("sse_read_timeout", 60 * 5),
            )
        )
        return sse_r, sse_w
