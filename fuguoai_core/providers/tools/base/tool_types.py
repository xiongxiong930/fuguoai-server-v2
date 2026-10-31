"""复果工具系统的基础类型定义（自有实现，clean-room 重写）。

本模块仅使用标准库，集中声明「工具来源类型」枚举与「工具定义」数据类，
供各类执行器与工具管理器共享，不引用任何第三方项目代码。
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional


class ToolType(Enum):
    """工具按其部署位置 / 接入方式划分的来源类型。"""

    SERVER_PLUGIN = "server_plugin"   # 服务端内置插件（本地函数）
    SERVER_MCP = "server_mcp"         # 服务端 MCP（远程 MCP 服务）
    DEVICE_IOT = "device_iot"         # 设备端 IoT（由设备注册的描述生成）
    DEVICE_MCP = "device_mcp"         # 设备端 MCP（设备自带 MCP 服务）
    MCP_ENDPOINT = "mcp_endpoint"     # MCP 接入点（外部中转端）


@dataclass
class ToolDefinition:
    """描述一个可被 LLM 调用的工具。

    ``description`` 采用 OpenAI function-calling 的 JSON Schema 形态；
    ``parameters`` 为兼容旧式调用而保留的可选原始参数描述。
    """

    name: str
    description: Dict[str, Any]
    tool_type: ToolType
    parameters: Optional[Dict[str, Any]] = None
