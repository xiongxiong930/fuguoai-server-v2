"""全局垃圾回收调度（复果自有实现）。

周期性在事件循环空闲时触发 ``gc.collect()``，避免对象堆积导致后续某次 GC 突然
停顿、抢占 GIL。设计要点：
- 单例对外暴露，全进程只有一条 GC 调度任务；
- 实际回收动作交线程池执行，不阻塞 asyncio 事件循环；
- stop() 通过取消任务即时退出，无需等待下一个周期。
"""

import asyncio
import gc
import threading
from fuguoai_config.logger import setup_logging

_LOG = setup_logging()

# 默认 GC 周期（秒）
_PERIOD = 300


class GlobalGCManager:
    """全局垃圾回收管理器：按固定间隔在后台执行 gc.collect()。"""

    def __init__(self, interval_seconds: int = _PERIOD):
        self.interval_seconds = interval_seconds
        self._task = None
        self._stop_event = asyncio.Event()
        self._lock = threading.Lock()

    async def start(self) -> None:
        """启动定时 GC 任务（重复调用仅告警一次）。"""
        if self._task is not None:
            _LOG.bind(tag=__name__).warning("GC 调度器已在运行，忽略重复启动")
            return
        _LOG.bind(tag=__name__).info(
            f"全局 GC 调度已启动，周期 {self.interval_seconds} 秒"
        )
        self._stop_event.clear()
        self._task = asyncio.create_task(self._cycle())

    async def stop(self) -> None:
        """停止定时 GC 任务并清理任务句柄。"""
        if self._task is None:
            return
        _LOG.bind(tag=__name__).info("停止全局 GC 调度")
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _cycle(self) -> None:
        """周期主循环：到点执行一次 GC，stop 时立即退出。"""
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.sleep(self.interval_seconds)
                except asyncio.CancelledError:
                    raise
                if self._stop_event.is_set():
                    break
                await self._sweep()
        except asyncio.CancelledError:
            _LOG.bind(tag=__name__).info("GC 调度循环已取消")
            raise
        except Exception as exc:  # noqa: BLE001 - 单轮异常不应终止调度
            _LOG.bind(tag=__name__).error(f"GC 调度循环异常: {exc}")
        finally:
            _LOG.bind(tag=__name__).info("GC 调度循环已退出")

    async def _sweep(self) -> None:
        """在线程池中执行一次 GC，并打点对象增减（debug 级）。"""
        loop = asyncio.get_running_loop()

        def _collect():
            with self._lock:
                before = len(gc.get_objects())
                collected = gc.collect()
                after = len(gc.get_objects())
                return before, collected, after

        try:
            before, collected, after = await loop.run_in_executor(None, _collect)
            _LOG.bind(tag=__name__).debug(
                f"GC 执行完成：回收 {collected} 个对象（{before} -> {after}）"
            )
        except Exception as exc:  # noqa: BLE001 - 单轮 GC 失败不影响调度
            _LOG.bind(tag=__name__).error(f"GC 执行异常: {exc}")


_INSTANCE = None


def get_gc_manager(interval_seconds: int = _PERIOD):
    """获取全局 GC 管理器单例。"""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = GlobalGCManager(interval_seconds)
    return _INSTANCE
