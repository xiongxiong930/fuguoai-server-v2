"""LLM 提供方工厂（复果自有实现）。

按配置里的 ``type`` 字段动态加载 ``fuguoai_core/providers/llm/<type>/<type>.py``，
取出其中的 ``LLMProvider`` 类并实例化。加载结果会缓存进 ``sys.modules``，
避免同一后端在多连接场景下被反复导入。

模块导入时会把项目根目录补进 ``sys.path``，保证以脚本方式启动时也能定位到包。
"""

import os
import sys

# 添加项目根目录到Python路径
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "..", ".."))
sys.path.insert(0, project_root)

from fuguoai_config.logger import setup_logging
import importlib

logger = setup_logging()


def create_instance(class_name, *args, **kwargs):
    """创建指定类型的 LLM 提供方实例。

    :param class_name: 提供方目录名，对应 ``providers/llm/<class_name>/<class_name>.py``
    :raises ValueError: 目录不存在时抛出，提示配置的 type 有误
    """
    # 创建LLM实例
    if os.path.exists(
        os.path.join("fuguoai_core", "providers", "llm", class_name, f"{class_name}.py")
    ):
        lib_name = f"fuguoai_core.providers.llm.{class_name}.{class_name}"
        if lib_name not in sys.modules:
            sys.modules[lib_name] = importlib.import_module(f"{lib_name}")
        return sys.modules[lib_name].LLMProvider(*args, **kwargs)

    raise ValueError(f"不支持的LLM类型: {class_name}，请检查该配置的type是否设置正确")
