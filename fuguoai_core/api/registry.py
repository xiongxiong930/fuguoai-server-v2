"""HTTP 路由注册中心（扩展点）。

问题背景
--------
早期所有路由都在 ``http_server.py`` 里逐个 ``add_routes`` 注册，随着功能模块
不断增加，那个方法膨胀成了谁都不敢碰的"上帝方法"：新增一个功能就要回去改
核心文件，合并冲突频发，功能之间也无法独立裁剪。

设计
----
把路由的所有权交还给各功能模块：模块自己定义 ``fn(app)``，用
``@register_routes`` 装饰器登记到本模块的 ``_ROUTERS`` 列表；``apply_routes``
在 HTTP 服务启动时按命名约定自动扫描并导入 ``*_routes.py``，随后统一回调所有
已登记的注册函数。核心代码因此不再需要知道任何具体功能的存在。

约定
----
1. 文件命名：路由文件一律以 ``*_routes.py`` 结尾，且不得以 ``__`` 开头；
   放在 ``api/`` 下或 ``api/features/`` 下均可被自动发现。
2. 模块内用 ``@register_routes`` 装饰一个签名为 ``fn(app)`` 的函数，
   在函数体里完成该模块自己的路由注册。
3. 用户态接口统一使用 ``/api/v1/<feature>`` 前缀，并且一律经过
   ``core.guards.api_guard`` 做鉴权与限流。
4. 数据落库不写在路由文件里，统一走 ``core/utils/<feature>_store.py``
   提供的存储层。

本模块只依赖标准库（``importlib`` / ``os``）；日志器在需要时才从
``fuguoai_config.logger`` 惰性导入，避免在导入期引入额外依赖。
"""

import importlib
import os

# 所有通过 @register_routes 登记的路由注册函数
_ROUTERS = []


def register_routes(fn):
    """装饰器：把 ``fn(app)`` 登记为路由注册函数。

    同一个函数被重复装饰时只登记一次；始终原样返回 ``fn``。
    """
    if fn not in _ROUTERS:
        _ROUTERS.append(fn)
    return fn


def _discover(pkg_name, base_dir=None):
    """扫描并导入 ``pkg_name`` 下所有符合命名约定的路由模块。

    ``base_dir`` 为 None 时取本模块文件所在目录（刻意不用包的 ``__file__``——
    命名空间包下它可能是 None）。``pkg_name`` 为 ``fuguoai_core.api`` 时直接扫
    ``base_dir``，否则扫 ``base_dir/features``。

    单个模块导入失败只会记 error 日志并跳过，不中断整体发现过程。
    """
    if base_dir is None:
        base_dir = os.path.dirname(os.path.abspath(__file__))

    if pkg_name == "fuguoai_core.api":
        scan_dir = base_dir
    else:
        scan_dir = os.path.join(base_dir, "features")

    if not os.path.isdir(scan_dir):
        return

    for filename in sorted(os.listdir(scan_dir)):
        if not filename.endswith("_routes.py"):
            continue
        if filename.startswith("__"):
            continue
        mod = "{}.{}".format(pkg_name, filename[:-3])
        try:
            importlib.import_module(mod)
        except Exception as e:
            from fuguoai_config.logger import setup_logging

            setup_logging().bind(tag=__name__).error(
                "功能路由模块导入失败(已跳过): {}: {}".format(mod, e)
            )


def apply_routes(app):
    """发现所有路由模块并把它们注册到 ``app`` 上。

    fail-soft：任何单个模块（无论是导入还是注册阶段）出错都只记 error 日志并
    跳过，不影响其它功能模块与核心路由。
    """
    from fuguoai_config.logger import setup_logging

    logger = setup_logging()

    _discover("fuguoai_core.api")
    try:
        _discover("fuguoai_core.api.features")
    except Exception:
        pass

    for fn in _ROUTERS:
        try:
            fn(app)
        except Exception as e:
            logger.error(
                "路由注册失败，已跳过该模块: {}.{}: {}".format(
                    getattr(fn, "__module__", "?"), fn.__name__, e
                )
            )
