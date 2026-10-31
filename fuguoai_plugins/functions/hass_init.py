"""Home Assistant 插件的公共初始化（复果自有实现）。

两件事：
1. 把用户在配置里登记的家庭设备清单追加进系统提示词，模型才知道有哪些
   设备可控制；
2. 从插件配置里取出 HA 的服务地址与 API Key，供三个 hass_* 工具调用。

配置来源有优先级：优先读 `plugins.home_assistant`，该节点缺失时回退到
`plugins.hass_get_state`（历史配置形态，需继续兼容）。
"""

from typing import TYPE_CHECKING

import httpx

from fuguoai_config.logger import setup_logging
from fuguoai_core.utils.util import check_model_key

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

# 设备清单提示词前缀：模型据此知道 entity_id 的取值来源
_DEVICE_LIST_PROMPT = (
    "\n下面是我家智能设备列表（位置，设备名，entity_id），可以通过homeassistant控制\n"
)

# 新配置节点名，缺失时回退的老节点名
_PRIMARY_NODE = "home_assistant"
_FALLBACK_NODE = "hass_get_state"

# check_model_key 的供应商标识
_KEY_PROVIDER = "home_assistant"


def _config_source(conn):
    """确定 HA 配置取自哪个插件节点。"""
    plugins_config = conn.config.get("plugins", {})
    return _PRIMARY_NODE if plugins_config.get(_PRIMARY_NODE) else _FALLBACK_NODE


def _enabled_functions(conn):
    """当前 Intent 模块启用的函数名清单。"""
    intent = conn.config["Intent"][conn.config["selected_module"]["Intent"]]
    return intent.get("functions", [])


def _hass_tools_enabled(conn):
    """只有 hass_get_state / hass_set_state 至少启用一个，才需要注入设备清单。"""
    funcs = _enabled_functions(conn)
    return _FALLBACK_NODE in funcs or "hass_set_state" in funcs


def append_devices_to_prompt(conn):
    """function_call 意图下，把已配置的设备清单追加到系统提示词。"""
    if conn.intent_type != "function_call":
        return
    if not _hass_tools_enabled(conn):
        return

    plugins_config = conn.config.get("plugins", {})
    listing = plugins_config.get(_config_source(conn), {}).get("devices", "")
    conn.prompt += _DEVICE_LIST_PROMPT + listing + "\n"
    conn.dialogue.update_system_message(conn.prompt)


def initialize_hass_handler(conn):
    """取出 HA 的 base_url / api_key；插件未启用或无配置时返回空字典。"""
    if not conn.load_function_plugin:
        return {}

    plugin_config = conn.config.get("plugins", {}).get(_config_source(conn))
    if not plugin_config:
        return {}

    ha_config = {
        "base_url": plugin_config.get("base_url"),
        "api_key": plugin_config.get("api_key"),
    }

    # API Key 缺失/占位时给出告警，但不阻断，让调用方照常发起请求由 HA 兜底报错
    key_msg = check_model_key(_KEY_PROVIDER, ha_config.get("api_key"))
    if key_msg:
        logger.bind(tag=TAG).error(key_msg)

    return ha_config


def _auth_headers(ha_config: dict) -> dict:
    """HA REST 接口的鉴权头。"""
    return {
        "Authorization": f"Bearer {ha_config.get('api_key')}",
        "Content-Type": "application/json",
    }


async def call_hass_service(conn, service_path: str, payload=None, timeout: float = 5.0):
    """向 HA 下发一次服务调用，返回 httpx 响应对象。

    payload 为 None 时走 GET（读状态），否则走 POST（下发服务）。
    三个 hass_* 工具共用这一处，鉴权头与超时只在这里维护。
    """
    ha_config = initialize_hass_handler(conn)
    url = f"{ha_config.get('base_url')}{service_path}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=3.0)) as client:
        headers = _auth_headers(ha_config)
        if payload is None:
            return await client.get(url, headers=headers)
        return await client.post(url, headers=headers, json=payload)
