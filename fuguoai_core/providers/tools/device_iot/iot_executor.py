"""设备端 IoT 工具执行器（复果自有实现）。

把设备上报的物模型能力桥接为 LLM 可调用工具：注册阶段为每个属性生成「查询」工具、
为每个方法生成「控制」工具，并建立 ``tool_name → (kind, device, sub)`` 路由表；
执行阶段直接查表定位，避免对 ``tool_name`` 做 ``split('_')`` 反解（设备名含下划线
会错切），并把状态查询 / 命令下发分别走 ``conn.iot_descriptors`` 与 ``conn.safe_send``。
"""
import asyncio
import json
from typing import Any, Dict, Tuple

from fuguoai_config.logger import setup_logging
from ..base import ToolType, ToolDefinition, ToolExecutor
from fuguoai_plugins.register import Action, ActionResponse

TAG = __name__
logger = setup_logging()


class DeviceIoTExecutor(ToolExecutor):
    """设备端 IoT 工具执行器。"""

    def __init__(self, conn):
        self.conn = conn
        self.iot_tools: Dict[str, ToolDefinition] = {}
        # tool_name -> (kind, device, sub) 路由表；kind ∈ {"get", "control"}
        self._route: Dict[str, Tuple[str, str, str]] = {}

    async def execute(
        self, conn, tool_name: str, arguments: Dict[str, Any]
    ) -> ActionResponse:
        if tool_name not in self.iot_tools:
            return ActionResponse(
                action=Action.NOTFOUND,
                response=f"IoT工具 {tool_name} 不存在",
            )
        routed = self._route.get(tool_name)
        if routed is None:
            return ActionResponse(
                action=Action.ERROR, response="无法解析IoT工具名称")

        kind, device, sub = routed
        try:
            if kind == "get":
                return await self._run_query(device, sub, arguments)
            return await self._run_control(device, sub, arguments)
        except Exception as exc:  # 显式记录而非静默吞掉，仍按失败回退
            logger.bind(tag=TAG).warning(f"IoT工具执行异常 {tool_name}: {exc}")
            return ActionResponse(
                action=Action.ERROR,
                response=arguments.get("response_failure", "操作失败"))

    async def _run_query(self, device, prop, arguments):
        value = await self._read_state(device, prop)
        if value is None:
            return ActionResponse(
                action=Action.ERROR,
                response=arguments.get("response_failure", f"无法获取{device}的状态"))
        template = arguments.get("response_success", "查询成功：{value}")
        return ActionResponse(
            action=Action.RESPONSE,
            response=template.replace("{value}", str(value)))

    async def _run_control(self, device, method, arguments):
        params = {
            k: v
            for k, v in arguments.items()
            if k not in ("response_success", "response_failure")
        }
        await self._dispatch(device, method, params)
        await asyncio.sleep(0.1)
        reply = arguments.get("response_success", "操作成功")
        for name, val in params.items():
            token = "{" + name + "}"
            if token in reply:
                reply = reply.replace(token, str(val))
            if "{value}" in reply:
                reply = reply.replace("{value}", str(val))
                break
        return ActionResponse(action=Action.REQLLM, result=reply)

    async def _read_state(self, device_name: str, property_name: str):
        for key, desc in self.conn.iot_descriptors.items():
            if key.lower() != device_name.lower():
                continue
            for prop in desc.properties:
                if prop["name"].lower() == property_name.lower():
                    return prop["value"]
        return None

    async def _dispatch(
        self, device_name: str, method_name: str, parameters: Dict[str, Any]
    ):
        for key, desc in self.conn.iot_descriptors.items():
            if key.lower() != device_name.lower():
                continue
            for method in desc.methods:
                if method["name"].lower() != method_name.lower():
                    continue
                command = {"name": key, "method": method["name"]}
                if parameters:
                    command["parameters"] = parameters
                payload = json.dumps({"type": "iot", "commands": [command]})
                await self.conn.safe_send(payload)
                return
        raise Exception(f"未找到设备{device_name}的方法{method_name}")

    def register_iot_tools(self, descriptors):
        """依据设备上报（不可信）的物模型注册工具，畸形条目跳过并告警。"""
        if not isinstance(descriptors, list):
            logger.bind(tag=TAG).warning(
                f"IoT descriptors 非 list(类型={type(descriptors).__name__})，跳过注册")
            return
        for item in descriptors:
            if not isinstance(item, dict):
                logger.bind(tag=TAG).warning(
                    f"IoT descriptor 非 dict(类型={type(item).__name__})，跳过")
                continue
            name = item.get("name")
            if not name:
                logger.bind(tag=TAG).warning("IoT descriptor 缺 name，跳过")
                continue
            desc_text = item.get("description", "")
            self._register_props(name, desc_text, item.get("properties"))
            self._register_methods(name, desc_text, item.get("methods"))

    def _register_props(self, device, desc_text, properties):
        if not isinstance(properties, dict):
            return
        for prop_name, prop_info in properties.items():
            if not isinstance(prop_info, dict):
                continue
            tool = f"get_{device.lower()}_{prop_name.lower()}"
            schema = {
                "type": "function",
                "function": {
                    "name": tool,
                    "description": f"查询{desc_text}的{prop_info.get('description', prop_name)}",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "response_success": {
                                "type": "string",
                                "description": "查询成功时的友好回复，必须使用{value}作为占位符表示查询到的值",
                            },
                            "response_failure": {
                                "type": "string",
                                "description": "查询失败时的友好回复",
                            },
                        },
                        "required": ["response_success", "response_failure"],
                    },
                },
            }
            self.iot_tools[tool] = ToolDefinition(
                name=tool, description=schema, tool_type=ToolType.DEVICE_IOT)
            self._route[tool] = ("get", device, prop_name)

    def _register_methods(self, device, desc_text, methods):
        if not isinstance(methods, dict):
            return
        for method_name, method_info in methods.items():
            if not isinstance(method_info, dict):
                continue
            tool = f"{device.lower()}_{method_name.lower()}"
            params, required = {}, []
            raw = method_info.get("parameters")
            if isinstance(raw, dict):
                for pname, pinfo in raw.items():
                    if not isinstance(pinfo, dict):
                        continue
                    params[pname] = {
                        "type": pinfo.get("type", "string"),
                        "description": pinfo.get("description", pname),
                    }
                    required.append(pname)
            params.update({
                "response_success": {"type": "string", "description": "操作成功时的友好回复"},
                "response_failure": {"type": "string", "description": "操作失败时的友好回复"},
            })
            required.extend(["response_success", "response_failure"])
            schema = {
                "type": "function",
                "function": {
                    "name": tool,
                    "description": f"{desc_text} - {method_info.get('description', method_name)}",
                    "parameters": {
                        "type": "object",
                        "properties": params,
                        "required": required,
                    },
                },
            }
            self.iot_tools[tool] = ToolDefinition(
                name=tool, description=schema, tool_type=ToolType.DEVICE_IOT)
            self._route[tool] = ("control", device, method_name)

    def get_tools(self) -> Dict[str, ToolDefinition]:
        """返回本执行器托管的所有工具（副本）。"""
        return self.iot_tools.copy()

    def has_tool(self, tool_name: str) -> bool:
        """判断指定工具是否由本执行器托管。"""
        return tool_name in self.iot_tools
