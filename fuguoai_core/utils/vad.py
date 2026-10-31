import importlib
import os
import sys

from fuguoai_core.providers.vad.base import VADProviderBase
from fuguoai_config.logger import setup_logging

TAG = __name__
logger = setup_logging()


def create_instance(class_name: str, *args, **kwargs) -> VADProviderBase:
    """工厂方法创建VAD实例。

    按需加载 ``fuguoai_core/providers/vad/<class_name>.py`` 并实例化其中的
    ``VADProvider``；模块首次加载后缓存在 ``sys.modules`` 中，后续调用直接复用。
    """
    if os.path.exists(os.path.join("fuguoai_core", "providers", "vad", f"{class_name}.py")):
        lib_name = f"fuguoai_core.providers.vad.{class_name}"
        if lib_name not in sys.modules:
            sys.modules[lib_name] = importlib.import_module(f"{lib_name}")
        return sys.modules[lib_name].VADProvider(*args, **kwargs)

    raise ValueError(f"不支持的VAD类型: {class_name}，请检查该配置的type是否设置正确")
