"""记忆服务提供者动态装载工厂（复果自有实现）。

按记忆类型名在 ``fuguoai_core/providers/memory/<type>/<type>.py`` 定位实现模块，
动态加载并返回其中的 ``MemoryProvider`` 实例。

实现要点：基于 ``__file__`` 推导实现模块的绝对路径，经 ``importlib.util`` 显式
装载并自行维护模块缓存，不依赖当前工作目录，也不与全局包导入状态耦合。
"""
import importlib.util
import os
import sys

from fuguoai_config.logger import setup_logging

logger = setup_logging()

# 记忆实现模块统一根目录：<本文件所在目录>/providers/memory
_PROVIDER_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "providers",
    "memory",
)


def _resolve_provider(type_name: str):
    """解析记忆类型对应的实现模块。

    返回 ``(模块全限定名, 绝对文件路径)``；当实现文件不存在时两者均为 ``None``。
    """
    file_path = os.path.join(_PROVIDER_DIR, type_name, f"{type_name}.py")
    if not os.path.isfile(file_path):
        return None, None
    qual_name = f"fuguoai_core.providers.memory.{type_name}.{type_name}"
    return qual_name, file_path


def create_instance(class_name, *args, **kwargs):
    """按类型名装载并返回记忆提供方实例。

    ``class_name`` 对应 ``providers/memory/<class_name>/<class_name>.py`` 中的
    ``MemoryProvider`` 类；其余位置/关键字参数原样透传给其构造器。类型不存在时
    抛出 ``ValueError``。
    """
    qual_name, file_path = _resolve_provider(class_name)
    if qual_name is None:
        raise ValueError(f"不支持的记忆服务类型: {class_name}")

    if qual_name in sys.modules:
        provider_module = sys.modules[qual_name]
    else:
        spec = importlib.util.spec_from_file_location(qual_name, file_path)
        provider_module = importlib.util.module_from_spec(spec)
        sys.modules[qual_name] = provider_module
        spec.loader.exec_module(provider_module)

    return provider_module.MemoryProvider(*args, **kwargs)
