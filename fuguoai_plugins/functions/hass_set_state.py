"""Home Assistant 设备状态设置（复果自有实现）。

模型调用 ``hass_set_state`` 时，本模块把「动作 + 可选入参」翻译成一次
Home Assistant 的服务调用：``POST /api/services/{domain}/{action}``。

翻译规则集中在两张表里：
- ``_OP_RULES``：动作 -> 回给模型的文案模板 / HA 服务名 / 附加入参；
- ``_DOMAIN_ACTION_OVERRIDE``：某些域（窗帘、扫地机、播放器）要用本域专属
  的服务名，而不是通用服务名。

两张表是数据与逻辑分离，新增动作只需加一行，不再堆叠 if-elif。
"""

import httpx
from fuguoai_config.logger import setup_logging
from fuguoai_plugins.functions.hass_init import call_hass_service
from fuguoai_plugins.register import register_function, ToolType, ActionResponse, Action
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

# LLM 工具契约：名称/描述/参数 schema 直接决定模型怎么调用，逐字保留，不可改
hass_set_state_function_desc = {
    "type": "function",
    "function": {
        "name": "hass_set_state",
        "description": "设置homeassistant里设备的状态,包括开、关,调整灯光亮度、颜色、色温,调整播放器的音量,设备的暂停、继续、静音操作",
        "parameters": {
            "type": "object",
            "properties": {
                "state": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "description": "需要操作的动作,打开设备:turn_on,关闭设备:turn_off,增加亮度:brightness_up,降低亮度:brightness_down,设置亮度:brightness_value,增加音量:volume_up,降低音量:volume_down,设置音量:volume_set,设置色温:set_kelvin,设置颜色:set_color,设备暂停:pause,设备继续:continue,静音/取消静音:volume_mute",
                        },
                        "input": {
                            "type": "integer",
                            "description": "只有在设置音量,设置亮度时候才需要,有效值为1-100,对应音量和亮度的1%-100%",
                        },
                        "is_muted": {
                            "type": "string",
                            "description": "只有在设置静音操作时才需要,设置静音的时候该值为true,取消静音时该值为false",
                        },
                        "rgb_color": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "只有在设置颜色时需要,这里填目标颜色的rgb值",
                        },
                    },
                    "required": ["type"],
                },
                "entity_id": {
                    "type": "string",
                    "description": "需要操作的设备id,homeassistant里的entity_id",
                },
            },
            "required": ["state", "entity_id"],
        },
    },
}

# 服务调用路径模板：{domain} 取 entity_id 的前缀，{action} 由规则表给出
_SERVICE_PATH = "/api/services/{domain}/{action}"


def _literal(value):
    """附加入参：取固定值。"""

    def getter(_state):
        return value

    return getter


def _from_state(field):
    """附加入参：原样取 state 里的字段。"""

    def getter(state):
        return state[field]

    return getter


def _percent(field):
    """附加入参：state 里给的是 1-100 百分比，HA 要 0-1 小数，统一换算。"""

    def getter(state):
        return state[field] / 100

    return getter


# 动作 -> (回给模型的文案模板, HA 服务名, 附加入参键 或 None, 取值器 或 None)
# 文案模板里的 {input} / {rgb} 由 _describe 填充
_OP_RULES = {
    "turn_on": ("设备已打开", "turn_on", None, None),
    "turn_off": ("设备已关闭", "turn_off", None, None),
    "brightness_up": ("灯光已调亮", "turn_on", "brightness_step_pct", _literal(10)),
    "brightness_down": ("灯光已调暗", "turn_on", "brightness_step_pct", _literal(-10)),
    "brightness_value": ("亮度已调整到{input}", "turn_on", "brightness_pct", _from_state("input")),
    "set_color": ("颜色已调整到{rgb}", "turn_on", "rgb_color", _from_state("rgb_color")),
    "set_kelvin": ("色温已调整到{input}K", "turn_on", "kelvin", _from_state("input")),
    "volume_up": ("音量已调大", "volume_up", None, None),
    "volume_down": ("音量已调小", "volume_down", None, None),
    "volume_set": ("音量已调整到{input}", "volume_set", "volume_level", _percent("input")),
    "volume_mute": ("设备已静音", "volume_mute", "is_volume_muted", _from_state("is_muted")),
    "pause": ("设备已暂停", "pause", None, None),
    "continue": ("设备已继续", "continue", None, None),
}

# (动作, 域) -> 该域专属的服务名，覆盖规则表里的默认服务名
_DOMAIN_ACTION_OVERRIDE = {
    ("turn_on", "cover"): "open_cover",
    ("turn_on", "vacuum"): "start",
    ("turn_off", "cover"): "close_cover",
    ("turn_off", "vacuum"): "stop",
    ("pause", "media_player"): "media_pause",
    ("pause", "cover"): "stop_cover",
    ("pause", "vacuum"): "pause",
    ("continue", "media_player"): "media_play",
    ("continue", "vacuum"): "start",
}


def _domain_of(entity_id: str):
    """entity_id 形如 domain.object；缺 "." 视为非法，返回 None。"""
    head, _, rest = entity_id.partition(".")
    return head if rest else None


def _describe(template: str, state: dict) -> str:
    """填充文案模板里的占位符；无占位符时原样返回。"""
    return template.format(input=state.get("input"), rgb=state.get("rgb_color"))


@register_function("hass_set_state", hass_set_state_function_desc, ToolType.SYSTEM_CTL)
async def hass_set_state(conn: "ConnectionHandler", entity_id="", state=None):
    if state is None:
        state = {}
    try:
        ha_response = await handle_hass_set_state(conn, entity_id, state)
        return ActionResponse(Action.REQLLM, ha_response, None)
    except httpx.TimeoutException:
        logger.bind(tag=TAG).error("设置Home Assistant状态超时")
        return ActionResponse(Action.ERROR, "请求超时", None)
    except Exception:
        error_msg = "执行Home Assistant操作失败"
        logger.bind(tag=TAG).error(error_msg)
        return ActionResponse(Action.ERROR, error_msg, None)


async def handle_hass_set_state(conn: "ConnectionHandler", entity_id, state):
    """把一次 hass_set_state 调用翻译成 Home Assistant 的 service 调用并下发。"""
    domain = _domain_of(entity_id)
    if domain is None:
        return "执行失败，错误的设备id"

    op = state["type"]
    rule = _OP_RULES.get(op)
    if rule is None:
        return f"{domain} {op}功能尚未支持"

    template, action, extra_key, extra_get = rule
    action = _DOMAIN_ACTION_OVERRIDE.get((op, domain), action)

    # 组装下发到 Home Assistant 的服务调用
    payload = {"entity_id": entity_id}
    if extra_key is not None:
        payload[extra_key] = extra_get(state)

    endpoint = _SERVICE_PATH.format(domain=domain, action=action)
    response = await call_hass_service(conn, endpoint, payload)

    description = _describe(template, state)
    logger.bind(tag=TAG).info(
        f"设置状态:{description},url:{endpoint},return_code:{response.status_code}"
    )
    if response.status_code == 200:
        return description
    return f"设置失败，错误码: {response.status_code}"
