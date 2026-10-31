"""ASR provider 工厂（复果自有实现）。

根据配置选中的 ASR provider 名称，按约定路径动态加载
``fuguoai_core.providers.asr.<class_name>`` 模块，并实例化其顶层 ``ASRProvider`` 类，
把剩余位置 / 关键字参数透传给构造器。模块文件不存在时抛 ``ValueError`` 提示配置错误。
"""

import importlib
import os
import sys

from fuguoai_config.logger import setup_logging
from fuguoai_core.providers.asr.base import ASRProviderBase

TAG = __name__
logger = setup_logging()


def create_instance(class_name: str, *args, **kwargs) -> ASRProviderBase:
    """工厂方法创建 ASR 实例。

    :param class_name: provider 模块名（不含 .py），如 ``tencent``。
    :param args / kwargs: 透传给目标 provider 构造器的参数。
    :raises ValueError: 当 ``fuguoai_core/providers/asr/<class_name>.py`` 不存在时。
    """
    module_path = os.path.join("fuguoai_core", "providers", "asr", f"{class_name}.py")
    if os.path.exists(module_path):
        lib_name = f"fuguoai_core.providers.asr.{class_name}"
        if lib_name not in sys.modules:
            sys.modules[lib_name] = importlib.import_module(lib_name)
        return sys.modules[lib_name].ASRProvider(*args, **kwargs)

    raise ValueError(f"不支持的ASR类型: {class_name}，请检查该配置的type是否设置正确")
