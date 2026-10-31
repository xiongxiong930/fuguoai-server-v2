"""设备间语音呼叫工具（复果自有实现）。

封装向管理面发起的「通讯录呼叫 / 接听来电」两类请求：
两者共用同一个 manager-api 入口，靠 answer 标记区分是主动呼叫还是接听。
对外契约（工具名、参数 schema、返回的 Action/ActionResponse）保持稳定。
"""

from typing import TYPE_CHECKING

import httpx

from fuguoai_config.logger import setup_logging
from fuguoai_plugins.register import (
    Action,
    ActionResponse,
    ToolType,
    register_function,
)

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

call_device_function_desc = {
    "type": "function",
    "function": {
        "name": "call_device",
        "description": (
            "用于设备之间建立语音通话连接。"
            "当用户说出以下意图时调用此工具：\n"
            "1. 主动呼叫：用户说”呼叫XX/打电话给XX/连线XX/打给XX/帮我呼叫XX”时调用，nickname取XX。"
            "例如：”呼叫张三”→nickname=”张三”、”帮我连线小陈”→nickname=”小陈”；\n"
            "2. 接听来电：系统刚提示”您收到来自XX的来电，是否接听？”后，用户说”接听/接通/同意接听/同意连线/同意对话”时调用，"
            "nickname取提示中的XX。\n"
            "如果用户输入不是明确接听，也不是明确拒绝，不得调用call_device，必须先追问一次"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "nickname": {"type": "string", "description": "目标设备的备注名，例如：小陈、小翁"},
            },
            "required": ["nickname"],
        },
    },
}


def _failure(message: str) -> ActionResponse:
    """把失败原因直接回给用户，不再绕道 LLM。"""
    return ActionResponse(action=Action.RESPONSE, response=message)


async def _call_manager(endpoint: str, query: dict, token: str) -> dict:
    """向管理面发送一次 GET 请求，并把响应解析成字典。"""
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
        reply = await client.get(endpoint, params=query, headers=headers)
    return reply.json()


def _is_answering(conn: "ConnectionHandler") -> bool:
    """有未接的来电待处理时，判定为接听模式。"""
    return getattr(conn, "incoming_call", None) is not None


@register_function("call_device", call_device_function_desc, ToolType.SYSTEM_CTL)
async def call_device(conn: "ConnectionHandler", nickname: str):
    """按备注名呼叫对方设备，或接听一通待接来电。"""
    device_id = conn.headers.get("device-id")
    if not device_id:
        return _failure("无法获取本机MAC地址")

    manager_cfg = conn.config.get("manager-api", {})
    base_url = manager_cfg.get("url")
    secret = manager_cfg.get("secret")
    if not base_url or not secret:
        logger.bind(tag=TAG).error("manager-api配置缺失")
        return _failure("配置错误，请稍后再试")

    answering = _is_answering(conn)
    query = {"callerMac": device_id, "nickname": nickname}
    if answering:
        query["answer"] = "true"

    try:
        payload = await _call_manager(
            f"{base_url}/device/address-book/call", query, secret
        )
    except httpx.HTTPError as exc:
        logger.bind(tag=TAG).error(f"呼叫请求失败: {exc}")
        return _failure("呼叫失败，请稍后再试")

    if payload.get("code") != 0:
        return _failure(payload.get("msg", "呼叫失败"))

    body = payload.get("data", {})
    if body.get("status") == "error":
        return _failure(body.get("message"))

    if answering:
        return ActionResponse(action=Action.NONE, response="已成功接听")

    conn.calling = True
    return ActionResponse(action=Action.NONE, response=f"正在呼叫{nickname}，请等待对方接听")
