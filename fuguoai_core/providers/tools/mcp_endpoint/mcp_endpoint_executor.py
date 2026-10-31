"""MCP 接入点工具执行器（复果自有实现）。

把接入点（外部 WebSocket JSON-RPC 通道）上暴露的工具桥接进复果工具执行框架：
由 :class:`ToolExecutor` 统一调度，结果再映射回 ``ActionResponse``。

行为约定（与接入点客户端协同，对外契约不变）：
* 客户端未初始化或未就绪时返回 ``Action.ERROR``；
* 结果若是带 ``action`` 字段的结构化回执，则直接映射回对应 ``Action``；
* 其余情况按 ``Action.REQLLM`` 回退，把原文交给上层 LLM；
* 参数解析失败（``ValueError``）映射为 ``Action.NOTFOUND``。
"""

import json
from typing import Any, Dict

from fuguoai_config.logger import setup_logging
from fuguoai_plugins.register import Action, ActionResponse
from ..base import ToolDefinition, ToolExecutor, ToolType
from .mcp_endpoint_handler import call_mcp_endpoint_tool

_MODULE = __name__
_LOG = setup_logging()


class MCPEndpointExecutor(ToolExecutor):
    """把接入点工具接入复果工具执行框架。"""

    def __init__(self, conn):
        self._peer = conn

    def _endpoint(self):
        """取出连接上的接入点客户端；未初始化时返回 None。"""
        candidate = getattr(self._peer, "mcp_endpoint_client", None)
        return candidate if candidate else None

    async def execute(
        self, conn, tool_name: str, arguments: Dict[str, Any]
    ) -> ActionResponse:
        """执行一个接入点工具。"""
        endpoint = self._endpoint()
        if endpoint is None:
            return ActionResponse(
                action=Action.ERROR,
                response="MCP接入点客户端未初始化",
            )

        if not await endpoint.is_ready():
            return ActionResponse(
                action=Action.ERROR,
                response="MCP接入点客户端未准备就绪",
            )

        try:
            envelope = _to_json(arguments)
            reply = await call_mcp_endpoint_tool(endpoint, tool_name, envelope)
            return self._coerce(reply)
        except ValueError as exc:
            return ActionResponse(action=Action.NOTFOUND, response=str(exc))
        except Exception as exc:
            return ActionResponse(action=Action.ERROR, response=str(exc))

    def _coerce(self, raw) -> ActionResponse:
        """把接入点返回的原始结果映射为 ActionResponse。"""
        structured = _try_parse(raw)
        if isinstance(structured, dict) and "action" in structured:
            return ActionResponse(
                action=Action[structured["action"]],
                response=structured.get("response", ""),
            )
        return ActionResponse(action=Action.REQLLM, result=str(raw))

    def get_tools(self) -> Dict[str, ToolDefinition]:
        """汇集接入点侧暴露的全部工具定义。"""
        endpoint = self._endpoint()
        if endpoint is None:
            return {}

        collected: Dict[str, ToolDefinition] = {}
        for entry in endpoint.get_available_tools():
            func_def = entry.get("function", {})
            name = func_def.get("name", "")
            if name:
                collected[name] = ToolDefinition(
                    name=name, description=entry, tool_type=ToolType.MCP_ENDPOINT
                )
        return collected

    def has_tool(self, tool_name: str) -> bool:
        """判断接入点侧是否提供指定工具。"""
        endpoint = self._endpoint()
        if endpoint is None:
            return False
        return endpoint.has_tool(tool_name)


def _to_json(arguments: Dict[str, Any]) -> str:
    """把调用参数序列化为 JSON 文本；空参数退化为 ``"{}"``。"""
    if not arguments:
        return "{}"
    return json.dumps(arguments)


def _try_parse(reply):
    """尝试把文本回执解析成对象；解析失败返回 ``None``，绝不抛错。"""
    if not isinstance(reply, str):
        return None
    try:
        return json.loads(reply)
    except Exception as err:
        _LOG.bind(tag=_MODULE).warning(f"MCP工具结果非JSON,按原文返回: {err}")
        return None
