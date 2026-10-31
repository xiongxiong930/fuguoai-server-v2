"""插件包自动导入（复果自有实现）。

按包路径扫描并导入其下所有顶层模块，用于触发插件模块内的装饰器注册。
"""

import importlib
import pkgutil

from fuguoai_config.logger import setup_logging

TAG = __name__

logger = setup_logging()


def auto_import_modules(package_name: str) -> None:
    """导入指定包下的全部顶层模块（不递归子包）。

    Args:
        package_name: 包名，如 "fuguoai_plugins.functions"。
    """
    package = importlib.import_module(package_name)
    for _, module_name, _ in pkgutil.iter_modules(package.__path__):
        importlib.import_module(f"{package_name}.{module_name}")
