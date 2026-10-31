"""通过 Home Assistant 的 Music Assistant 集成播放音乐（复果自有实现）。

模型调用 ``hass_play_music`` 时，本模块向 HA 的 ``music_assistant/play_media``
服务下发播放指令。成功时返回的提示语会经 ``Action.RECORD`` 播报给用户，
因此两句对外文案（成功 / 带错误码的失败）保持原样，避免改变听感。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fuguoai_config.logger import setup_logging
from fuguoai_plugins.functions.hass_init import call_hass_service
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

hass_play_music_function_desc = {
    "type": "function",
    "function": {
        "name": "hass_play_music",
        "description": "用户想听音乐、有声书的时候使用，在房间的媒体播放器（media_player）里播放对应音频",
        "parameters": {
            "type": "object",
            "properties": {
                "media_content_id": {
                    "type": "string",
                    "description": "可以是音乐或有声书的专辑名称、歌曲名、演唱者,如果未指定就填random",
                },
                "entity_id": {
                    "type": "string",
                    "description": "需要操作的音箱的设备id,homeassistant里的entity_id,media_player开头",
                },
            },
            "required": ["media_content_id", "entity_id"],
        },
    },
}

# Music Assistant 的播放服务路径
_PLAY_SERVICE_PATH = "/api/services/music_assistant/play_media"

# 播放比状态读写慢，超时放宽到 10 秒
_PLAY_TIMEOUT = 10.0


@register_function(
    "hass_play_music", hass_play_music_function_desc, ToolType.SYSTEM_CTL
)
async def hass_play_music(
    conn: "ConnectionHandler", entity_id: str = "", media_content_id: str = "random"
):
    """把 LLM 的音乐播放意图转成 HA Music Assistant 播放指令。"""
    try:
        playback_hint = await handle_hass_play_music(conn, entity_id, media_content_id)
    except Exception as exc:  # 顶层兜底，避免异常外泄到对话主链路
        logger.bind(tag=TAG).error(f"处理音乐意图错误: {exc}")
        return ActionResponse(
            action=Action.RESPONSE, result=str(exc), response="播放音乐时出错了"
        )
    return ActionResponse(
        action=Action.RECORD, result="指令已接收", response=playback_hint
    )


async def handle_hass_play_music(
    conn: "ConnectionHandler", entity_id: str, media_content_id: str
) -> str:
    """向 Home Assistant 下发一次播放请求，返回给用户的可读提示。"""
    payload = {"entity_id": entity_id, "media_id": media_content_id}
    response = await call_hass_service(
        conn, _PLAY_SERVICE_PATH, payload, timeout=_PLAY_TIMEOUT
    )
    if response.status_code == 200:
        return f"正在播放{media_content_id}的音乐"
    return f"音乐播放失败，错误码: {response.status_code}"
