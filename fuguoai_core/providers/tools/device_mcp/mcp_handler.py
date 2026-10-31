"""设备端 MCP 通道的消息收发层。

职责：
- 把工具调用请求包成 JSON-RPC 信封经安全通道发给设备，并等待回包；
- 收到设备的 initialize / tools/list 应答后，把工具登记进本地 ``MCPClient``；
- 处理设备回的 method / error 主动帧。

线格式（JSON-RPC 2.0 + ``{"type":"mcp","payload":...}`` 外壳）与设备固件对齐，
字段名、method 字符串、msg id 约定属于契约，不可改动。
"""

import json
import asyncio
import re
from typing import TYPE_CHECKING

from fuguoai_core.utils.util import get_vision_url, sanitize_tool_name
from fuguoai_core.utils.auth import AuthToken
from fuguoai_config.logger import setup_logging

from .mcp_client import MCPClient

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler

_LOG = setup_logging()
_MODULE = __name__

# 与设备约定的两条固定 msg id
_INIT_MSG_ID = 1
_LIST_MSG_ID = 2


async def send_mcp_message(conn: "ConnectionHandler", payload: dict):
    """把 payload 裹进 mcp 外壳，经安全通道下发；设备不支持则跳过。"""
    if not conn.features.get("mcp"):
        _LOG.bind(tag=_MODULE).warning("对端未声明 mcp 能力，丢弃本次下发")
        return

    envelope = json.dumps({"type": "mcp", "payload": payload})
    try:
        await conn.safe_send(envelope)
        _LOG.bind(tag=_MODULE).debug(f"已下发 mcp 帧: {envelope}")
    except Exception as exc:  # 下发失败不阻断主流程，仅记录
        _LOG.bind(tag=_MODULE).error(f"mcp 帧下发异常: {exc}")


def _build_envelope(method: str, msg_id: int, params: dict = None) -> dict:
    """统一构造 JSON-RPC 2.0 请求信封。"""
    frame = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        frame["params"] = params
    return frame


async def _route_result(conn: "ConnectionHandler", mcp_client: MCPClient, payload: dict):
    """分发带 result 的响应：工具调用回执 / 初始化应答 / 工具清单。"""
    outcome = payload["result"]
    identity = int(payload.get("id", 0))

    if identity in mcp_client.call_results:
        _LOG.bind(tag=_MODULE).debug(f"工具调用回执到达，序号 {identity}")
        await mcp_client.resolve_call_result(identity, outcome)
        return

    if identity == _INIT_MSG_ID:
        _record_server_meta(outcome)
        await asyncio.sleep(1)
        await send_mcp_tools_list_request(conn)
        return

    if identity == _LIST_MSG_ID:
        await _absorb_catalog(conn, mcp_client, outcome)


def _record_server_meta(outcome: dict):
    """记录设备侧 MCP server 的自描述信息（仅日志用途）。"""
    info = outcome.get("serverInfo")
    if isinstance(info, dict):
        _LOG.bind(tag=_MODULE).debug(
            f"对端 server: name={info.get('name')} version={info.get('version')}"
        )


async def _absorb_catalog(conn: "ConnectionHandler", mcp_client: MCPClient, outcome):
    """把 tools/list 的应答登记进 MCPClient，处理分页与就绪信号。"""
    if not isinstance(outcome, dict) or "tools" not in outcome:
        return
    entries = outcome["tools"]
    if not isinstance(entries, list):
        _LOG.bind(tag=_MODULE).error("tools/list 应答格式非法")
        return

    _LOG.bind(tag=_MODULE).info(f"对端暴露工具数: {len(entries)}")
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        spec = _normalize_schema(entry)
        await mcp_client.add_tool(spec)
        _LOG.bind(tag=_MODULE).debug(f"登记工具 #{index + 1}: {spec['name']}")

    _swap_names_in_descriptions(mcp_client)

    cursor = outcome.get("nextCursor", "")
    if cursor:
        await send_mcp_tools_list_continue_request(conn, cursor)
        return

    await mcp_client.set_ready(True)
    _LOG.bind(tag=_MODULE).debug("工具集就绪，触发本地刷新")
    handler = getattr(conn, "func_handler", None)
    if handler:
        handler.tool_manager.refresh_tools()
        handler.current_support_functions()


def _normalize_schema(entry: dict) -> dict:
    """从设备回的单个工具描述中抽取规整后的 schema。"""
    raw_name = entry.get("name", "")
    raw_desc = entry.get("description", "")
    shape = {"type": "object", "properties": {}, "required": []}
    raw = entry.get("inputSchema")
    if isinstance(raw, dict):
        shape["type"] = raw.get("type", "object")
        shape["properties"] = raw.get("properties", {})
        shape["required"] = [s for s in raw.get("required", []) if isinstance(s, str)]
    return {"name": raw_name, "description": raw_desc, "inputSchema": shape}


def _swap_names_in_descriptions(mcp_client: MCPClient):
    """把工具描述里出现的原始名替换为 sanitized 名，保证 LLM 与调用一致。"""
    for blob in mcp_client.tools.values():
        if "description" not in blob:
            continue
        text = blob["description"]
        for sanitized, original in mcp_client.name_mapping.items():
            text = text.replace(original, sanitized)
        blob["description"] = text


async def _route_error(mcp_client: MCPClient, payload: dict):
    """分发带 error 的响应，把对应 Future 兑现为异常。"""
    fault = payload["error"]
    reason = fault.get("message", "未知错误")
    _LOG.bind(tag=_MODULE).error(f"收到 MCP 错误帧: {reason}")
    identity = int(payload.get("id", 0))
    if identity in mcp_client.call_results:
        await mcp_client.reject_call_result(identity, Exception(f"MCP错误: {reason}"))


async def handle_mcp_message(
    conn: "ConnectionHandler", mcp_client: MCPClient, payload: dict
):
    """入口：按 payload 形态分流到 result / method / error 三类处理。"""
    if not isinstance(payload, dict):
        _LOG.bind(tag=_MODULE).error("MCP 帧缺少 payload 或类型错误")
        return

    if "result" in payload:
        await _route_result(conn, mcp_client, payload)
    elif "method" in payload:
        _LOG.bind(tag=_MODULE).info(f"收到 MCP 主动请求: {payload['method']}")
    elif "error" in payload:
        await _route_error(mcp_client, payload)


async def send_mcp_initialize_message(conn: "ConnectionHandler"):
    """下发 initialize 请求，附带视觉服务地址与设备短期鉴权 token。"""
    endpoint = get_vision_url(conn.config)
    token = AuthToken(conn.config["server"]["auth_key"]).generate_token(
        conn.headers.get("device-id")
    )
    vision_block = {"url": endpoint, "token": token}
    envelope = _build_envelope(
        "initialize",
        _INIT_MSG_ID,
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "roots": {"listChanged": True},
                "sampling": {},
                "vision": vision_block,
            },
            "clientInfo": {"name": "FuguoaiClient", "version": "1.0.0"},
        },
    )
    _LOG.bind(tag=_MODULE).debug("下发 initialize 帧")
    await send_mcp_message(conn, envelope)


async def send_mcp_tools_list_request(conn: "ConnectionHandler"):
    """请求设备返回完整工具清单（首页）。"""
    _LOG.bind(tag=_MODULE).debug("下发 tools/list 请求")
    await send_mcp_message(conn, _build_envelope("tools/list", _LIST_MSG_ID))


async def send_mcp_tools_list_continue_request(conn: "ConnectionHandler", cursor: str):
    """工具清单分页：带上次返回的 cursor 取下一页。"""
    _LOG.bind(tag=_MODULE).info(f"下发带 cursor 的 tools/list: {cursor}")
    await send_mcp_message(
        conn, _build_envelope("tools/list", _LIST_MSG_ID, {"cursor": cursor})
    )


def _normalize_arguments(args):
    """把上层传入的 args（str / dict）规整成 dict。

    字符串入参先做 JSON 解析；模型有时一次吐出多个 JSON 对象，
    此时逐个解析后合并。    解析失败时抛出 ``ValueError``，文案沿用历史
    以保证对外契约稳定（含历史上既有的二次包裹形式，行为须与既有实现保持一致）。
    """
    if isinstance(args, str):
        if not args.strip():
            return {}
        try:
            return json.loads(args)
        except json.JSONDecodeError:
            try:
                json_objects = re.findall(r"\{[^{}]*\}", args)
                if len(json_objects) > 1:
                    merged = {}
                    for piece in json_objects:
                        try:
                            obj = json.loads(piece)
                            if isinstance(obj, dict):
                                merged.update(obj)
                        except json.JSONDecodeError:
                            continue
                    if merged:
                        return merged
                    raise ValueError(f"无法解析任何有效的JSON对象: {args}")
                raise ValueError(f"参数JSON解析失败: {args}")
            except Exception as exc:
                _LOG.bind(tag=_MODULE).error(
                    f"参数JSON解析失败: {str(exc)}, 原始参数: {args}"
                )
                raise ValueError(f"参数JSON解析失败: {str(exc)}")
    if isinstance(args, dict):
        return args
    raise ValueError(f"参数类型错误，期望字符串或字典，实际类型: {type(args)}")


async def call_mcp_tool(
    conn: "ConnectionHandler",
    mcp_client: MCPClient,
    tool_name: str,
    args: str = "{}",
    timeout: int = 30,
):
    """调用设备上的一个 MCP 工具，阻塞等到响应或超时。

    返回工具回的文本内容；若回的是结构化 content，则取第一段 text。
    """
    if not await mcp_client.is_ready():
        raise RuntimeError("MCP客户端尚未准备就绪")

    if not mcp_client.has_tool(tool_name):
        raise ValueError(f"工具 {tool_name} 不存在")

    call_id = await mcp_client.get_next_id()
    pending = asyncio.Future()
    await mcp_client.register_call_result_future(call_id, pending)

    try:
        arguments = _normalize_arguments(args)
        if not isinstance(arguments, dict):
            raise ValueError(f"参数必须是字典类型，实际类型: {type(arguments)}")
    except Exception as exc:
        if not isinstance(exc, ValueError):
            raise ValueError(f"参数处理失败: {str(exc)}")
        raise exc

    # 调用时用设备原始名，描述里 sanitized 过的名只给 LLM 看
    on_device = mcp_client.name_mapping.get(tool_name, tool_name)
    envelope = _build_envelope(
        "tools/call",
        call_id,
        {"name": on_device, "arguments": arguments},
    )
    _LOG.bind(tag=_MODULE).info(f"下发工具调用: {on_device}，参数 {args}")
    await send_mcp_message(conn, envelope)

    try:
        reply = await asyncio.wait_for(pending, timeout=timeout)
        _LOG.bind(tag=_MODULE).info(f"工具 {on_device} 调用成功，原始回执 {reply}")
        if isinstance(reply, dict):
            if reply.get("isError") is True:
                detail = reply.get("error", "工具调用返回错误，但未提供具体错误信息")
                raise RuntimeError(f"工具调用错误: {detail}")
            content = reply.get("content")
            if isinstance(content, list) and content and isinstance(content[0], dict) and "text" in content[0]:
                return content[0]["text"]
        return str(reply)
    except asyncio.TimeoutError:
        await mcp_client.cleanup_call_result(call_id)
        raise TimeoutError("工具调用请求超时")
    except Exception as exc:
        await mcp_client.cleanup_call_result(call_id)
        raise exc
