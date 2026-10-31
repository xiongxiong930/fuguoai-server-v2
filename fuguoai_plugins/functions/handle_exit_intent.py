"""退出意图处理（复果自有实现）。

LLM 判定用户想结束对话时调用 ``handle_exit_intent``：本模块把连接标记为
「对话结束后关闭」，并回送一段告别语。对外文案与连接态改动属于契约，原样
保留；本文件只重写了导入组织、常量提取与注释表达。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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

# 未提供告别语时的默认收尾话术
DEFAULT_GOODBYE = "再见，祝您生活愉快！"

handle_exit_intent_function_desc = {
    "type": "function",
    "function": {
        "name": "handle_exit_intent",
        "description": "当用户想结束对话或需要退出系统时调用",
        "parameters": {
            "type": "object",
            "properties": {
                "say_goodbye": {
                    "type": "string",
                    "description": "和用户友好结束对话的告别语",
                }
            },
            "required": ["say_goodbye"],
        },
    },
}


@register_function(
    "handle_exit_intent", handle_exit_intent_function_desc, ToolType.SYSTEM_CTL
)
def handle_exit_intent(conn: "ConnectionHandler", say_goodbye: str | None = None):
    """标记连接为对话结束后关闭，并回送告别语。"""
    try:
        if say_goodbye is None:
            say_goodbye = DEFAULT_GOODBYE

        # 对话结束后主动断开，避免设备空转等待
        if not conn.close_after_chat:
            conn.close_after_chat = True

        logger.bind(tag=TAG).info(f"退出意图已处理:{say_goodbye}")
        return ActionResponse(
            action=Action.RESPONSE, result="退出意图已处理", response=say_goodbye
        )
    except Exception as exc:  # 顶层兜底，避免异常外泄到对话主链路
        logger.bind(tag=TAG).error(f"处理退出意图错误: {exc}")
        return ActionResponse(
            action=Action.NONE, result="退出意图处理失败", response=""
        )
