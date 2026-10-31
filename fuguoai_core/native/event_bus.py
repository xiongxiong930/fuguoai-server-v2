"""进程内、跨线程的内存事件桥。

把设备同步线程里的实时事件，安全投递到 HTTP 侧 asyncio 订阅者的队列里。
本模块为 clean-room 独立重写，仅依赖 Python 标准库，不 import 任何
fuguoai_core 其它模块，也不引入第三方依赖。
"""

import asyncio
import threading
import time
from itertools import count

# 每个订阅者队列的容量上限（上游测试直接 import 此常量）。
QUEUE_MAXSIZE = 200


class EventBus:
    """跨线程事件桥。

    发布方可从任意线程调用 ``publish(event)``；订阅者在某个 asyncio
    event loop 内通过 ``subscribe()`` 拿到一个 ``asyncio.Queue``，在协程里
    ``await queue.get()`` 收事件，供小程序面板实时刷新。
    """

    def __init__(self) -> None:
        # 订阅表：sid -> _Subscriber(loop, queue)
        self._subs: dict[int, "_Subscriber"] = {}
        # 保证 sid 分配与订阅表读写在多线程下安全。
        self._lock = threading.Lock()
        # sid 序列生成器（线程安全，配合锁使用）。
        self._sid_gen = count(1)

    def subscribe(self) -> tuple[int, asyncio.Queue]:
        """在当前运行中的 event loop 内调用（如 WS handler 协程内）。

        记录该 loop，返回一个 ``(sid:int, queue:asyncio.Queue)`` 元组。
        使用 ``asyncio.get_running_loop()`` 获取当前 loop（兜底
        ``asyncio.get_event_loop()``）。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        with self._lock:
            sid = next(self._sid_gen)
            self._subs[sid] = _Subscriber(loop=loop, queue=queue)
        return sid, queue

    def unsubscribe(self, sid: int) -> None:
        """移除订阅；未知 sid 静默忽略，不抛异常。"""
        with self._lock:
            self._subs.pop(sid, None)

    def has_subscribers(self) -> bool:
        """当前是否存在订阅者。"""
        with self._lock:
            return bool(self._subs)

    def publish(self, event: dict) -> None:
        """从任意线程调用皆可，把事件广播给全部订阅者。

        - event 为假值（None/空）时立即返回，不抛、不阻塞；
        - 无订阅者时立即返回 no-op；
        - event 缺 ``'ts'`` 键时补 ``int(time.time())``；
        - 对每个订阅者，用其所属 loop 的 ``call_soon_threadsafe`` 把事件投递进其 queue；
        - 单个订阅者投递异常（如 loop 已关闭）仅影响该订阅者，被吞掉，
          不影响发布方与其它订阅者。
        """
        if not event:
            return
        # 无订阅者时直接 no-op，零开销。
        with self._lock:
            if not self._subs:
                return
            # 快照订阅者，避免在投递过程中长期持锁。
            snapshot = list(self._subs.values())
        if "ts" not in event:
            event["ts"] = int(time.time())
        for sub in snapshot:
            try:
                sub.loop.call_soon_threadsafe(EventBus._deliver, sub.queue, event)
            except Exception:
                # 单个订阅者投递失败（如 loop 已关闭）不影响其它订阅者。
                pass

    @staticmethod
    def _deliver(queue: asyncio.Queue, event: dict) -> None:
        """在订阅者所属 loop 线程内执行，把事件放进队列。

        - 正常：``queue.put_nowait(event)``；
        - 队列满（asyncio.QueueFull）：先 ``get_nowait()`` 丢最旧，再 ``put_nowait(event)``；
        - 任何异常均吞掉，不让慢消费者影响发布方。
        """
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()  # 丢最旧
            except Exception:
                pass
            try:
                queue.put_nowait(event)
            except Exception:
                pass
        except Exception:
            pass


class _Subscriber:
    """内部辅助结构：记录某个订阅者所属的 loop 与其队列。"""

    __slots__ = ("loop", "queue")

    def __init__(self, loop, queue: asyncio.Queue) -> None:
        self.loop = loop
        self.queue = queue


# 模块级单例。
_bus = None
_bus_lock = threading.Lock()


def get_event_bus() -> EventBus:
    """返回全局唯一的 EventBus 实例（模块级单例）。"""
    global _bus
    if _bus is None:
        with _bus_lock:
            if _bus is None:
                _bus = EventBus()
    return _bus
