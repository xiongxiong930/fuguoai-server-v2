"""Home Assistant 设备状态查询（复果自有实现）。

模型调用 ``hass_get_state`` 查询某个 entity 的当前状态，本模块向 HA 的
``/api/states/{entity_id}`` 取回实体对象，再把其中模型关心的几个属性
（曲名、音量、色温、RGB、亮度）拼成一句自然语言交回模型组织回复。

属性按固定顺序输出，顺序本身是对模型输出稳定性的约束，不要随意调整。
"""

from typing import TYPE_CHECKING

import httpx

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

hass_get_state_function_desc = {
    "type": "function",
    "function": {
        "name": "hass_get_state",
        "description": "获取homeassistant里设备的状态,包括查询灯光亮度、颜色、色温,媒体播放器的音量,设备的暂停、继续操作",
        "parameters": {
            "type": "object",
            "properties": {
                "entity_id": {
                    "type": "string",
                    "description": "需要操作的设备id,homeassistant里的entity_id",
                }
            },
            "required": ["entity_id"],
        },
    },
}

# 实体状态接口路径模板
_STATE_PATH = "/api/states/{entity_id}"

# (属性名, 中文标签)：仅输出实体确实带有的属性，且按此顺序拼装
_REPORTED_ATTRS = (
    ("media_title", "正在播放的是"),
    ("volume_level", "音量是"),
    ("color_temp_kelvin", "色温是"),
    ("rgb_color", "rgb颜色是"),
    ("brightness", "亮度是"),
)


@register_function("hass_get_state", hass_get_state_function_desc, ToolType.SYSTEM_CTL)
async def hass_get_state(conn: "ConnectionHandler", entity_id=""):
    """查询设备状态，结果交回 LLM 组织回复。"""
    try:
        return ActionResponse(
            Action.REQLLM, await handle_hass_get_state(conn, entity_id), None
        )
    except httpx.TimeoutException:
        logger.bind(tag=TAG).error("获取Home Assistant状态超时")
        return ActionResponse(Action.ERROR, "请求超时", None)
    except Exception:
        error_msg = "执行Home Assistant操作失败"
        logger.bind(tag=TAG).error(error_msg)
        return ActionResponse(Action.ERROR, error_msg, None)


async def handle_hass_get_state(conn: "ConnectionHandler", entity_id):
    """调用 HA 的 /api/states 取单个实体状态，拼成给 LLM 的文本。"""
    response = await call_hass_service(
        conn, _STATE_PATH.format(entity_id=entity_id)
    )
    if response.status_code != 200:
        return f"切换失败，错误码: {response.status_code}"

    entity = response.json()
    logger.bind(tag=TAG).info(f"api返回内容: {entity}")

    responsetext = _render_state(entity)
    logger.bind(tag=TAG).info(f"查询返回内容: {responsetext}")
    return responsetext


def _render_state(entity: dict) -> str:
    """把实体对象渲染成给模型的一句话状态描述。"""
    attributes = entity.get("attributes", {})
    chunks = [f"设备状态:{entity['state']} "]
    chunks.extend(
        f"{label}:{attributes[name]} "
        for name, label in _REPORTED_ATTRS
        if name in attributes
    )
    return "".join(chunks)
