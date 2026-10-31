"""插件注册框架（复果自有实现）。

插件模块通过装饰器把函数登记进全局注册表，运行期由 FunctionRegistry 按名取用。
设备侧 IOT 函数另按「设备能力签名」归类管理。

对外公开的符号（ToolType / Action / ActionResponse / FunctionItem /
DeviceTypeRegistry / FunctionRegistry / register_function / register_device_function /
all_function_registry）及其行为保持稳定，仅为内部实现做了重写。
"""

from enum import Enum

from fuguoai_config.logger import setup_logging

TAG = __name__
logger = setup_logging()


class ToolType(Enum):
    """工具调用后的后续动作类型。"""

    NONE = (1, "调用完工具后，不做其他操作")
    WAIT = (2, "调用工具，等待函数返回")
    CHANGE_SYS_PROMPT = (3, "修改系统提示词，切换角色性格或职责")
    SYSTEM_CTL = (
        4,
        "系统控制，影响正常的对话流程，如退出、播放音乐等，需要传递conn参数",
    )
    IOT_CTL = (5, "IOT设备控制，需要传递conn参数")
    MCP_CLIENT = (6, "MCP客户端")

    def __init__(self, code, message):
        self.code = code
        self.message = message


class Action(Enum):
    """工具/插件执行结果要求的后续处置。"""

    ERROR = (-1, "错误")
    NOTFOUND = (0, "没有找到函数")
    NONE = (1, "啥也不干")
    RESPONSE = (2, "直接回复")
    REQLLM = (3, "调用函数后再请求llm生成回复")
    RECORD = (4, "记录工具调用到对话历史，不调用LLM")

    def __init__(self, code, message):
        self.code = code
        self.message = message


class ActionResponse:
    """插件执行结果。"""

    def __init__(self, action: Action, result=None, response=None):
        self.action = action        # 要求的后续动作
        self.result = result        # 动作产生的结果
        self.response = response    # 直接回复的内容


class FunctionItem:
    """注册表中的一个函数条目。"""

    def __init__(self, name, description, func, type):
        self.name = name
        self.description = description
        self.func = func
        self.type = type


class DeviceTypeRegistry:
    """IOT 设备类型注册表：按设备能力签名归类其可用函数。"""

    def __init__(self):
        self.type_functions = {}  # type_signature -> {func_name: FunctionItem}

    def generate_device_type_id(self, descriptor):
        """用设备的属性名与方法名组合生成类型签名，作为类型 ID。"""
        properties = sorted(descriptor["properties"].keys())
        methods = sorted(descriptor["methods"].keys())
        return f"{descriptor['name']}:{','.join(properties)}:{','.join(methods)}"

    def get_device_functions(self, type_id):
        """取指定设备类型的全部函数，未注册时返回空字典。"""
        return self.type_functions.get(type_id, {})

    def register_device_type(self, type_id, functions):
        """登记设备类型及其函数；同类型只登记一次，不覆盖已有条目。"""
        if type_id not in self.type_functions:
            self.type_functions[type_id] = functions


# 全局函数注册表：装饰器写入，FunctionRegistry 按名取用
all_function_registry = {}


def register_function(name, desc, type=None):
    """把函数注册进全局注册表的装饰器。"""

    def decorator(func):
        all_function_registry[name] = FunctionItem(name, desc, func, type)
        logger.bind(tag=TAG).debug(f"函数 '{name}' 已加载，可以注册使用")
        return func

    return decorator


def register_device_function(name, desc, type=None):
    """设备级函数装饰器：仅做加载记录，不进入全局函数表。"""

    def decorator(func):
        logger.bind(tag=TAG).debug(f"设备函数 '{name}' 已加载")
        return func

    return decorator


class FunctionRegistry:
    """按名管理当前启用的函数集合。"""

    def __init__(self):
        self.function_registry = {}
        self.logger = setup_logging()

    def register_function(self, name, func_item=None):
        """登记函数：可直接给 FunctionItem，也可按名从全局表查找。"""
        if func_item:
            self.function_registry[name] = func_item
            self.logger.bind(tag=TAG).debug(f"函数 '{name}' 直接注册成功")
            return func_item

        func = all_function_registry.get(name)
        if not func:
            self.logger.bind(tag=TAG).error(f"函数 '{name}' 未找到")
            return None
        self.function_registry[name] = func
        self.logger.bind(tag=TAG).debug(f"函数 '{name}' 注册成功")
        return func

    def unregister_function(self, name):
        """注销函数，不存在时返回 False。"""
        if name not in self.function_registry:
            self.logger.bind(tag=TAG).error(f"函数 '{name}' 未找到")
            return False
        self.function_registry.pop(name, None)
        self.logger.bind(tag=TAG).info(f"函数 '{name}' 注销成功")
        return True

    def get_function(self, name):
        """按名取函数条目，不存在返回 None。"""
        return self.function_registry.get(name)

    def get_all_functions(self):
        """返回全部已登记函数（name -> FunctionItem）。"""
        return self.function_registry

    def get_all_function_desc(self):
        """返回全部函数的描述列表。"""
        return [func.description for _, func in self.function_registry.items()]
