"""IoT 设备物模型描述（复果自有实现）。

设备首次上报物模型（descriptors）时，后端将其归一成 :class:`IotDescriptor`：
属性与可调用方法各存一份列表，供 :class:`DeviceIoTExecutor` 据此注册 LLM 工具并执行。
本类仅做数据建模，不涉及任何 IO。属性未上报实时值时的默认占位按类型给定
（number→0 / boolean→False / 其余→空串）。
"""
from typing import Any, Dict, List, Optional

from fuguoai_config.logger import setup_logging

_TAG = __name__
logger = setup_logging()

# 类型 -> 默认占位值（未收到实时值时使用）
_TYPE_DEFAULTS = {"number": 0, "boolean": False}

# 工具描述截断上限（字符）。设备上报的物模型描述常超长，而 function-calling 的 tools
# 参数会在每轮对话的 LLM 请求中全量注入 prompt，造成 token 浪费。截断到合理长度
# 既保留核心语义，又压低 prompt token（2026-08-28 费用归因）。
_DESC_LIMIT = 120


def _clip(text) -> str:
    """截断物模型描述，避免超长文本每轮堆进 LLM prompt。"""
    if text is None:
        return ""
    if not isinstance(text, str):
        return str(text)
    text = text.strip()
    if len(text) <= _DESC_LIMIT:
        return text
    return text[:_DESC_LIMIT].rstrip() + "…"


class IotDescriptor:
    """单台 IoT 设备的物模型描述。"""

    def __init__(
        self,
        name: str,
        description: str,
        properties: Optional[Dict[str, Any]],
        methods: Optional[Dict[str, Any]],
    ) -> None:
        self.name = name
        self.description = description
        self.properties: List[Dict[str, Any]] = []
        self.methods: List[Dict[str, Any]] = []

        # 由上报的属性描述构造属性清单（带类型默认占位）。
        # 上报数据来自设备（不可信），逐条 .get() + 类型校验，畸形条目跳过而非抛错。
        if properties is None:
            properties = {}
        elif not isinstance(properties, dict):
            properties = {}
        for prop_key, prop_spec in properties.items():
            if not isinstance(prop_spec, dict):
                continue
            self.properties.append({
                "name": prop_key,
                "description": _clip(prop_spec.get("description", "")),
                "value": _TYPE_DEFAULTS.get(prop_spec.get("type", ""), ""),
            })

        # 由上报的方法描述构造方法清单（顺带拆出参数签名）。
        if methods is None:
            methods = {}
        elif not isinstance(methods, dict):
            methods = {}
        for method_key, method_spec in methods.items():
            if not isinstance(method_spec, dict):
                continue
            method = {
                "name": method_key,
                "description": _clip(method_spec.get("description", "")),
            }
            params = method_spec.get("parameters")
            if isinstance(params, dict):
                method["parameters"] = {
                    param_key: {
                        "type": param_spec.get("type", "string"),
                        "description": _clip(param_spec.get("description", "")),
                    }
                    for param_key, param_spec in params.items()
                    if isinstance(param_spec, dict)
                }
            self.methods.append(method)
