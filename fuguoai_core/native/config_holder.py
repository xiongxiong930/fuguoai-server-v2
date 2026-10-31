"""全局共享配置单例（零重启热加载核心）。

本模块是 ``config_holder`` 的全新独立实现，仅依据行为规格书编写。

职责：
- 持有唯一的共享配置 dict（``_CONFIG``），所有调用方持有同一引用。
- ``set_config`` 以**原地**方式（clear + update）填充该 dict，引用保持不变。
- ``reload`` 重新从磁盘加载并原地更新共享 dict，实现零重启生效，同时保留
  运行时派生的若干字段（auth_key / mcp_endpoint / read_config_from_api）。
"""

import asyncio
import os
import time

from fuguoai_config.config_loader import load_config, get_project_dir
from fuguoai_config.logger import setup_logging
from fuguoai_core.utils.cache.manager import cache_manager, CacheType

# 注意：必须与历史调用一致——setup_logging() 不带参（config=None 时才走真实配置加载）。
# 传 __name__ 会被当成 config 字符串，导致 config["log"] 崩溃。
logger = setup_logging()

# 配置文件的磁盘路径；set_config 时记录其 mtime 用于 watch 比对。
_CONFIG_PATH = get_project_dir() + "data/.config.yaml"

# 模块级唯一真相源：所有持有者共享同一个 dict 对象。
_CONFIG: dict = {}

# 最近一次 reload 的结果记录（初始为中性占位，与历史实现结构对齐）。
_last_reload: dict = {"ok": None, "trigger": None, "elapsed_ms": None, "keys": None, "ts": None}

# 最近一次观测到的配置文件 mtime；供 watchdog 比对。
_last_mtime: float | None = None

# 正在运行的 watchdog 异步任务（若有）。
_watch_task: "asyncio.Future | None" = None

# 串行化 reload，避免并发重载互相覆盖。
_reload_lock = asyncio.Lock()


def set_config(config: dict) -> None:
    """原地注入配置到共享 dict，并记录配置文件 mtime。

    使用 ``clear()`` + ``update()`` 保持对象引用不变，所有持有者自动见新内容。
    """
    # 记录配置文件 mtime，文件不可读时记 None（不阻塞注入）。
    try:
        mtime = os.path.getmtime(_CONFIG_PATH)
    except OSError:
        mtime = None
        logger.warning("无法读取配置文件 mtime: %s", _CONFIG_PATH)
    _CONFIG.clear()
    _CONFIG.update(config)


def get_config() -> dict:
    """返回共享配置 dict（始终为同一个对象）。"""
    return _CONFIG


async def reload(trigger: str = "manual") -> dict:
    """重新从磁盘加载配置并原地更新共享 dict，返回结构化结果。

    失败时保留旧配置不变，返回 ok=False 的结果，绝不向外抛异常。
    """
    t0 = time.time()
    async with _reload_lock:
        # 1. 捕获运行时派生的字段（reload 不应覆盖）。
        auth_key = _CONFIG.get("server", {}).get("auth_key")
        mcp_endpoint = _CONFIG.get("mcp_endpoint")
        read_config_from_api = _CONFIG.get("read_config_from_api", False)

        # 2. 失效配置缓存。
        cache_manager.delete(CacheType.CONFIG, "main_config")

        try:
            # 3. 重新从磁盘加载。
            fresh = await load_config()
        except Exception as e:  # 加载失败：保留旧配置，记录失败。
            elapsed_ms = round((time.time() - t0) * 1000, 1)
            result = {
                "ok": False,
                "trigger": trigger,
                "error": str(e),
                "elapsed_ms": elapsed_ms,
                "ts": int(time.time()),
            }
            _last_reload.update(result)
            return result

        # 4. 重放派生字段到 fresh。
        if auth_key:
            fresh.setdefault("server", {})["auth_key"] = auth_key
        if mcp_endpoint:
            if "/mcp/" in mcp_endpoint:
                fresh["mcp_endpoint"] = mcp_endpoint.replace("/mcp/", "/call/")
            else:
                fresh["mcp_endpoint"] = mcp_endpoint
        if read_config_from_api:
            fresh["read_config_from_api"] = True

        # 5. 原地更新共享 dict（保持引用）。
        _CONFIG.clear()
        _CONFIG.update(fresh)

        # 6. 返回结构化结果并合并进 _last_reload。
        elapsed_ms = round((time.time() - t0) * 1000, 1)
        result = {
            "ok": True,
            "trigger": trigger,
            "elapsed_ms": elapsed_ms,
            "keys": len(fresh),
            "ts": int(time.time()),
        }
        _last_reload.update(result)
        return result


def get_last_reload() -> dict:
    """返回最近一次 reload 结果的副本 dict。"""
    return dict(_last_reload)


async def _watchdog(interval: float = 5.0) -> None:
    """后台循环：轮询配置文件 mtime，变化时自动 reload。

    - 文件不可读（OSError）时跳过当次。
    - 首次观测到 mtime 仅记录，不触发。
    - mtime 与上次不同则更新记录并触发 reload(trigger="watchdog")。
    """
    global _last_mtime
    while True:
        await asyncio.sleep(interval)
        try:
            mtime = os.path.getmtime(_CONFIG_PATH)
        except OSError:
            continue
        if _last_mtime is None:
            _last_mtime = mtime
            continue
        if mtime != _last_mtime:
            _last_mtime = mtime
            await reload(trigger="watchdog")


def start_watchdog(interval: float = 5.0):
    """启动 watchdog 后台任务；若已运行则不重复启动。"""
    global _watch_task
    if _watch_task is None or _watch_task.done():
        _watch_task = asyncio.ensure_future(_watchdog(interval))
    return _watch_task
