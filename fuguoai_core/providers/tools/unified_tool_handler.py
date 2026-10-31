"""对话侧统一工具入口（复果原生实现，clean-room 重写）。

把 LLM 下发的 function call 分派给对应执行器，并把结果归一为 ActionResponse
回灌对话链路。支持 SERVER_PLUGIN / SERVER_MCP / DEVICE_IOT / DEVICE_MCP /
MCP_ENDPOINT 五类来源；多调用并发执行、单点失败隔离。
"""

from typing import Any, Dict, List, Optional

from fuguoai_config.logger import setup_logging
from fuguoai_core.handle.sendAudioHandle import send_display_message
from fuguoai_plugins.loadplugins import auto_import_modules
from fuguoai_plugins.register import Action as _ACT, ActionResponse as _RESP

from .base import ToolType as _KIND
from .server_plugins import ServerPluginExecutor
from .server_mcp import ServerMCPExecutor
from .device_iot import DeviceIoTExecutor
from .device_mcp import DeviceMCPExecutor
from .mcp_endpoint import MCPEndpointExecutor, connect_mcp_endpoint
from .unified_tool_manager import ToolManager, _sanitize_error

import asyncio
import json


class UnifiedToolHandler:
    """统一工具处理器。"""

    def __init__(self, conn):
        self.logger = setup_logging()
        self.conn = conn
        self.tool_manager = ToolManager(conn)
        self._plugin_ex = ServerPluginExecutor(conn)
        self._mcp_srv_ex = ServerMCPExecutor(conn)
        self._iot_ex = DeviceIoTExecutor(conn)
        self._mcp_dev_ex = DeviceMCPExecutor(conn)
        self._endpoint_ex = MCPEndpointExecutor(conn)
        self.config = conn.config
        self.ready = False

        # 按来源类型把执行器一次性注册进同一管理器。
        for kind, ex in [
            (_KIND.SERVER_PLUGIN, self._plugin_ex),
            (_KIND.SERVER_MCP, self._mcp_srv_ex),
            (_KIND.DEVICE_IOT, self._iot_ex),
            (_KIND.DEVICE_MCP, self._mcp_dev_ex),
            (_KIND.MCP_ENDPOINT, self._endpoint_ex),
        ]:
            self.tool_manager.register_executor(kind, ex)

    def get_functions(self):
        """返回所有工具的函数描述列表。"""
        return self.tool_manager.get_function_descriptions()

    def list_functions(self):
        """返回当前支持的函数名称列表并记录日志。"""
        names = self.tool_manager.get_supported_tool_names()
        self.logger.info(f"已装载函数: {names}")
        return names

    def upload_functions_desc(self):
        """刷新函数描述列表。"""
        self.tool_manager.refresh_tools()
        self.logger.info("函数描述已刷新")

    def has_tool(self, name: str) -> bool:
        """判断是否存在名为 ``name`` 的工具。"""
        return self.tool_manager.has_tool(name)

    def get_tool_statistics(self):
        """返回工具统计信息。"""
        return self.tool_manager.get_tool_statistics()

    async def register_iot_tools(self, descriptors: List[Dict[str, Any]]):
        """注册 IoT 设备工具并刷新工具表。"""
        self._iot_ex.register_iot_tools(descriptors)
        self.tool_manager.refresh_tools()
        self.logger.info(f"iot tools registered: {len(descriptors)}")

    async def cleanup(self):
        """清理资源：关闭 MCP 相关连接。"""
        try:
            await self._mcp_srv_ex.cleanup()
            ep_client = getattr(self.conn, "mcp_endpoint_client", None)
            if ep_client:
                await ep_client.close()
            self.logger.info("tool handler cleaned")
        except Exception as exc:
            self.logger.error(f"tool handler cleanup failed: {exc}")

    async def _initialize(self):
        """异步初始化：导入插件、拉起 MCP 接入点与 Home Assistant 提示。"""
        try:
            auto_import_modules("fuguoai_plugins.functions")
            await self._mcp_srv_ex.initialize()
            await self._connect_endpoint()
            self._setup_hass()

            self.ready = True
            self.logger.debug("tool handler ready")
            self.list_functions()
        except Exception as exc:
            self.logger.error(f"tool handler init failed: {exc}")

    async def _connect_endpoint(self):
        """按需连接 MCP 接入点（URL 配置合法时才尝试）。"""
        cfg = self.config
        endpoint_url = cfg.get("mcp_endpoint") if cfg else None
        if not endpoint_url or "你的" in endpoint_url or endpoint_url == "null":
            return
        self.logger.info(f"connecting mcp endpoint: {endpoint_url}")
        try:
            ep_client = await connect_mcp_endpoint(endpoint_url, self.conn)
        except Exception as exc:
            self.logger.error(f"mcp endpoint init failed: {exc}")
            return
        if ep_client:
            self.conn.mcp_endpoint_client = ep_client
            self.logger.info("mcp endpoint ok")
        else:
            self.logger.warning("mcp endpoint down")

    def _setup_hass(self):
        """把 Home Assistant 设备信息追加进提示词（可选能力）。"""
        try:
            from fuguoai_plugins.functions.hass_init import append_devices_to_prompt

            append_devices_to_prompt(self.conn)
        except ImportError:
            pass
        except Exception as exc:
            self.logger.error(f"hass init failed: {exc}")

    def _combine_responses(self, items: List[_RESP]) -> _RESP:
        """合并多个 function call 的响应为单个 ActionResponse。"""
        if not items:
            return _RESP(action=_ACT.NONE, response="无响应")

        bodies: List[str] = []
        notes: List[str] = []
        for each in items:
            # 任一错误则直接返回首个错误响应。
            if each.action == _ACT.ERROR:
                return each
            if each.result:
                bodies.append(each.result)
            if each.response:
                notes.append(each.response)

        kind = (
            _ACT.REQLLM
            if any(entry.action == _ACT.REQLLM for entry in items)
            else _ACT.RESPONSE
        )
        return _RESP(
            action=kind,
            result="; ".join(bodies) or None,
            response="; ".join(notes) or None,
        )

    async def _run_batch(self, batch_calls) -> _RESP:
        """并发执行一批 function call：延迟降为最慢单次，单点失败被隔离。"""
        tasks = [
            self.tool_manager.execute_tool(item["name"], item.get("arguments", {}))
            for item in batch_calls
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        out: List[_RESP] = []
        for r in results:
            if isinstance(r, Exception):
                self.logger.error(f"batch call error: {r}")
                out.append(_RESP(action=_ACT.ERROR, response=_sanitize_error(str(r))))
            else:
                out.append(r)
        return self._combine_responses(out)

    async def _run_single(self, payload: Dict[str, Any]) -> _RESP:
        """处理单个 function call：解析参数、下发显示消息、执行工具。"""
        name = payload["name"]
        args = payload.get("arguments", {})

        if isinstance(args, str):
            if not args:
                args = {}
            else:
                try:
                    args = json.loads(args)
                except Exception:
                    self.logger.error(f"参数解析失败: {args}")
                    return _RESP(action=_ACT.ERROR, response="函数参数不是合法 JSON")
        elif args is None:
            args = {}

        self.logger.debug(f"call {name} args={args}")

        try:
            await send_display_message(self.conn, f"% {name}")
        except Exception as exc:
            self.logger.warning(f"display msg send failed: {exc}")

        return await self.tool_manager.execute_tool(name, args)

    async def handle_llm_function_call(
        self, conn, req: Dict[str, Any]
    ) -> Optional[_RESP]:
        """处理 LLM 下发的 function call（支持单调用与并发多调用）。"""
        try:
            batch = req.get("function_calls")
            if batch:
                return await self._run_batch(batch)
            return await self._run_single(req)
        except Exception as exc:
            self.logger.error(f"llm function call failed: {exc}")
            return _RESP(action=_ACT.ERROR, response=_sanitize_error(str(exc)))
