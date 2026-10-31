"""设备端 MCP 工具执行器。

当设备侧自带 MCP 服务时，设备在 hello 协商阶段把工具清单上传，
本执行器负责把这些工具转交设备端 MCP 执行，并把返回结果统一
规整为 :class:`ActionResponse`，交由上层 LLM 继续对话。

普通工具结果按 ``REQLLM`` 处理，由 LLM 基于结果组织回答；
若设备返回了带有 ``action`` 字段的结构化结果，则直接采用其语义。
"""

import json
from typing import Dict, Any, Optional, TYPE_CHECKING

from fuguoai_config.logger import setup_logging

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler
from ..base import ToolType, ToolDefinition, ToolExecutor
from fuguoai_plugins.register import Action, ActionResponse
from .mcp_handler import call_mcp_tool

_TAG = __name__
_log = setup_logging()


def _marshal_arguments(arguments: Dict[str, Any]) -> str:
    """把上层下发的参数对象序列化为设备端 MCP 期望的 JSON 文本。"""
    if not arguments:
        return "{}"
    return json.dumps(arguments)


def _safe_load(text: str):
    """尽力把文本解析为对象；解析失败返回 ``None``，绝不抛错。"""
    try:
        return json.loads(text)
    except Exception:
        return None


class DeviceMCPExecutor(ToolExecutor):
    """设备侧 MCP 工具的统一调度入口。"""

    def __init__(self, peer):
        self._peer = peer

    def _resolve_client(self, peer) -> Optional[object]:
        candidate = getattr(peer, "mcp_client", None)
        return candidate if candidate else None

    def _not_initialized(self) -> ActionResponse:
        return ActionResponse(
            action=Action.ERROR,
            response="设备端MCP客户端未初始化",
        )

    def _not_ready(self) -> ActionResponse:
        return ActionResponse(
            action=Action.ERROR,
            response="设备端MCP客户端未准备就绪",
        )

    async def execute(
        self, peer: "ConnectionHandler", name: str, args: Dict[str, Any]
    ) -> ActionResponse:
        """执行设备端 MCP 工具并把结果规整为动作响应。"""
        client = self._resolve_client(peer)
        if client is None:
            return self._not_initialized()

        if not await client.is_ready():
            return self._not_ready()

        try:
            payload = _marshal_arguments(args)
            raw = await call_mcp_tool(peer, client, name, payload)
        except ValueError as exc:
            return ActionResponse(action=Action.NOTFOUND, response=str(exc))
        except Exception as exc:
            return ActionResponse(action=Action.ERROR, response=str(exc))

        return self._coerce_reply(raw)

    def _coerce_reply(self, raw) -> ActionResponse:
        """把设备返回的原始结果归一为动作响应。"""
        if not isinstance(raw, str):
            return ActionResponse(action=Action.REQLLM, result=str(raw))
        parsed = _safe_load(raw)
        if isinstance(parsed, dict) and "action" in parsed:
            return ActionResponse(
                action=Action[parsed["action"]],
                response=parsed.get("response", ""),
            )
        return ActionResponse(action=Action.REQLLM, result=raw)

    def get_tools(self) -> Dict[str, ToolDefinition]:
        """枚举设备端 MCP 暴露的全部工具。"""
        client = self._resolve_client(self._peer)
        if client is None:
            return {}

        catalog: Dict[str, ToolDefinition] = {}
        for spec in client.get_available_tools():
            fn = spec.get("function", {})
            label = fn.get("name", "")
            if label:
                catalog[label] = ToolDefinition(
                    name=label,
                    description=spec,
                    tool_type=ToolType.DEVICE_MCP,
                )
        return catalog

    def has_tool(self, name: str) -> bool:
        """判断给定工具名是否由设备端 MCP 提供。"""
        client = self._resolve_client(self._peer)
        return client.has_tool(name) if client is not None else False
