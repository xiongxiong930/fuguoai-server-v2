"""VLLM 提供方工厂（复果自有实现）。

按配置里的 ``type`` 字段动态加载 ``fuguoai_core/providers/vllm/<type>.py``，
取出其中的 ``VLLMProvider`` 类并实例化。加载结果会缓存进 ``sys.modules``，
避免同一后端在多连接场景下被反复导入。

与 LLM 工厂的区别仅在目录层级：VLLM 后端是「一层目录 + 同名文件」，
LLM 后端是「目录 + 子目录 + 同名文件」。

模块导入时会把项目根目录补进 ``sys.path``，保证以脚本方式启动时也能定位到包。
"""

import importlib
import os
import sys

# 以脚本方式启动时，把项目根目录补进 sys.path，保证能定位到 fuguoai_core 包。
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
sys.path.insert(0, _ROOT)


def create_instance(class_name, *args, **kwargs):
    """按 ``type`` 动态加载并实例化一个 VLLM 提供方。

    :param class_name: 提供方模块名，对应 ``providers/vllm/<class_name>.py``
    :raises ValueError: 模块不存在时抛出，提示配置的 type 有误
    """
    # 动态加载 VLLM 后端（路径约定由 providers/vllm 目录结构锁定）
    if os.path.exists(
        os.path.join("fuguoai_core", "providers", "vllm", f"{class_name}.py")
    ):
        lib_name = f"fuguoai_core.providers.vllm.{class_name}"
        if lib_name not in sys.modules:
            sys.modules[lib_name] = importlib.import_module(f"{lib_name}")
        return sys.modules[lib_name].VLLMProvider(*args, **kwargs)

    raise ValueError(f"不支持的 VLLM 类型: {class_name}，请检查该配置的 type 是否设置正确")
