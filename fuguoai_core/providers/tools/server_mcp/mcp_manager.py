"""服务端 MCP 管理器（连接级，复果自有实现）。

复果为每个设备连接持有一个 :class:`ServerMCPManager`，负责：
* 读取 ``data/.mcp_server_settings.json`` 里的 ``mcpServers`` 配置；
* 并发拉起多个 stdio / HTTP MCP 服务客户端，并聚合它们的工具清单；
* 提供带「重试 + 断线重连」的工具调用通道；
* 在连接关闭 / 进程退出时统一回收所有客户端。

相对早期实现，这里固化了两处稳定性行为（重写时不可破坏）：
* ``initialize_servers`` 用 ``return_exceptions=True`` 隔离单个服务初始化
  失败，不让一个坏服务取消其余服务的加载；
* ``cleanup_all`` 把冗余的超时分支化简为单一 ``Exception`` 分支，单个
  客户端关闭失败被吞掉且不向上传播。
"""
import asyncio
import json
import os
from typing import Any, Dict, List, Optional

from mcp.types import LoggingMessageNotificationParams

from fuguoai_config.config_loader import get_project_dir
from fuguoai_config.logger import setup_logging
from .mcp_client import ServerMCPClient

_LOG = setup_logging()
_MODULE = __name__

_SETTINGS_REL = "data/.mcp_server_settings.json"
_INIT_TIMEOUT = 10
_MAX_RETRIES = 3
_CLOSE_TIMEOUT = 20
# 门会改写这个模块全局来加速重试测试
_RETRY_INTERVAL_S = 2


class ServerMCPManager:
    """管理某个连接下所有服务端 MCP 服务的集中管理器。"""

    def __init__(self, conn) -> None:
        self.conn = conn
        self._settings_path = get_project_dir() + _SETTINGS_REL
        if not os.path.exists(self._settings_path):
            self._settings_path = ""
            _LOG.bind(tag=_MODULE).warning(
                f"未找到mcp服务配置文件，请检查：{_SETTINGS_REL}"
            )
        self.clients: Dict[str, ServerMCPClient] = {}
        self.tools: List[Dict[str, Any]] = []
        self._spawn_lock = asyncio.Lock()

    # ---- 配置与工具查询 ----

    def load_config(self) -> Dict[str, Any]:
        if not self._settings_path:
            return {}
        try:
            with open(self._settings_path, "r", encoding="utf-8") as handle:
                raw_cfg = json.load(handle)
            return raw_cfg.get("mcpServers", {})
        except Exception as exc:
            _LOG.bind(tag=_MODULE).error(
                f"Error loading MCP config from {self._settings_path}: {exc}"
            )
            return {}

    def get_all_tools(self) -> List[Dict[str, Any]]:
        return self.tools

    def is_mcp_tool(self, tool_name: str) -> bool:
        for entry in self.tools:
            fn = entry.get("function")
            if fn is not None and fn.get("name") == tool_name:
                return True
        return False

    # ---- 并发拉起 ----

    async def initialize_servers(self) -> None:
        servers = self.load_config()
        pending = []
        for srv_name, srv_cfg in servers.items():
            if srv_cfg.get("command") or srv_cfg.get("url"):
                pending.append(self._spawn_one(srv_name, srv_cfg))
            else:
                _LOG.bind(tag=_MODULE).warning(
                    f"Skipping server {srv_name}: "
                    f"neither command nor url specified"
                )
        if pending:
            # 隔离单个服务初始化失败，不让一个坏服务取消其余服务的加载。
            await asyncio.gather(*pending, return_exceptions=True)
        func_handler = getattr(self.conn, "func_handler", None)
        if func_handler:
            tool_manager = getattr(func_handler, "tool_manager", None)
            if tool_manager:
                tool_manager.refresh_tools()
            func_handler.current_support_functions()

    async def _spawn_one(self, name: str, srv_cfg: Dict[str, Any]) -> None:
        client: Optional[ServerMCPClient] = None
        try:
            _LOG.bind(tag=_MODULE).info(f"拉起服务端MCP客户端 {name}")
            client = ServerMCPClient(srv_cfg)
            await asyncio.wait_for(
                client.initialize(logging_callback=self.logging_callback),
                timeout=_INIT_TIMEOUT,
            )
            async with self._spawn_lock:
                self.clients[name] = client
                self.tools.extend(client.get_available_tools())
        except asyncio.TimeoutError:
            _LOG.bind(tag=_MODULE).error(
                f"服务端MCP客户端 {name} 初始化超时"
            )
            if client:
                await client.cleanup()
        except Exception as exc:
            _LOG.bind(tag=_MODULE).error(
                f"服务端MCP客户端 {name} 初始化失败: {exc}"
            )
            if client:
                await client.cleanup()

    # ---- 工具调用（带重试 + 重连）----

    def _locate(self, tool_name: str):
        for srv_name, srv_client in self.clients.items():
            if srv_client.has_tool(tool_name):
                return srv_name, srv_client
        return None, None

    async def execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        _LOG.bind(tag=_MODULE).info(
            f"调用服务端MCP工具 {tool_name}，入参: {arguments}"
        )
        owner, target = self._locate(tool_name)
        if target is None:
            raise ValueError(f"工具 {tool_name} 在任意MCP服务中未找到")
        attempt = 0
        while attempt < _MAX_RETRIES:
            try:
                return await target.call_tool(
                    tool_name, arguments, progress_callback=self.progress_callback
                )
            except Exception as exc:
                if attempt == _MAX_RETRIES - 1:
                    raise
                _LOG.bind(tag=_MODULE).warning(
                    f"服务端MCP工具 {tool_name} 第 "
                    f"{attempt+1}/{_MAX_RETRIES} 次调用失败: {exc}"
                )
                owner, target = await self._reconnect(owner)
                await asyncio.sleep(_RETRY_INTERVAL_S)
                attempt += 1

    async def _reconnect(self, owner: Optional[str]):
        if owner is None:
            return None, None
        _LOG.bind(tag=_MODULE).info(
            f"准备重连服务端MCP客户端 {owner}"
        )
        target = self.clients.get(owner)
        if target is not None:
            try:
                await target.cleanup()
            except Exception as err:
                _LOG.bind(tag=_MODULE).error(
                    f"重连服务端MCP客户端 {owner} 失败: {err}"
                )
        cfg = self.load_config()
        if owner not in cfg:
            _LOG.bind(tag=_MODULE).error(
                f"无法重连服务端MCP客户端 {owner}：配置缺失"
            )
            return None, None
        reborn = ServerMCPClient(cfg[owner])
        await reborn.initialize(logging_callback=self.logging_callback)
        self.clients[owner] = reborn
        _LOG.bind(tag=_MODULE).info(f"服务端MCP客户端 {owner} 重连成功")
        return owner, reborn

    # ---- 回收 ----

    async def cleanup_all(self) -> None:
        for name, client in list(self.clients.items()):
            try:
                if hasattr(client, "cleanup"):
                    await asyncio.wait_for(
                        client.cleanup(), timeout=_CLOSE_TIMEOUT
                    )
                _LOG.bind(tag=_MODULE).info(f"已回收服务端MCP客户端 {name}")
            # 单一 Exception 分支：客户端关闭失败被吞掉且不向上传播。
            except Exception as exc:
                _LOG.bind(tag=_MODULE).error(
                    f"关闭服务端MCP客户端 {name} 时出错: {exc}"
                )
        self.clients.clear()

    # ---- MCP 协议回调（按引用传给客户端）----

    async def logging_callback(self, params: LoggingMessageNotificationParams) -> None:
        _LOG.bind(tag=_MODULE).info(
            f"MCP服务端日志[{params.level.upper()}]: {params.data}"
        )

    async def progress_callback(
        self, progress: float, total: Optional[float], message: Optional[str]
    ) -> None:
        _LOG.bind(tag=_MODULE).info(
            f"MCP进度 {progress}/{total}: {message}"
        )
