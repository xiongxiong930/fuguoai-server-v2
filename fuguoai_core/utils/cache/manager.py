"""复果全局缓存（进程内、线程安全）。

设计：
  - 每个 CacheType 一个 _Space（独立 OrderedDict + 独立锁 + 独立策略），
    互不阻塞；空间惰性创建。
  - 条目 = (value, expire_at, insert_time)。过期采用"读时惰性删 + 写时周期清扫"。
  - LRU 用 OrderedDict.move_to_end；FIFO 直接按插入序 popitem(last=False)。
  - 对外只有单例 cache_manager 与 get/set/delete/clear 四个动作。

用法：
    from fuguoai_core.utils.cache.manager import cache_manager, CacheType
    cache_manager.set(CacheType.WEATHER, "上海", report)
    report = cache_manager.get(CacheType.WEATHER, "上海")
"""

import time
import threading
from collections import OrderedDict
from typing import Any, Optional

from .config import CacheType, CachePolicy, policy_for

# 兼容旧 import 路径：from ...cache.manager import CacheType 亦可用
__all__ = ["cache_manager", "CacheType"]


class _Space:
    """单一缓存类型的存储空间。"""

    __slots__ = ("policy", "entries", "lock", "last_sweep")

    def __init__(self, policy: CachePolicy):
        self.policy = policy
        self.entries: "OrderedDict[str, tuple]" = OrderedDict()
        self.lock = threading.RLock()
        self.last_sweep = time.time()

    # -- 内部动作（调用方需持有 self.lock） --

    def _alive(self, key: str) -> bool:
        """条目存在且未过期；过期即顺手删除。"""
        item = self.entries.get(key)
        if item is None:
            return False
        expire_at = item[1]
        if expire_at is not None and time.time() >= expire_at:
            del self.entries[key]
            return False
        return True

    def _evict_if_full(self):
        while len(self.entries) > self.policy.max_size:
            self.entries.popitem(last=False)  # LRU 与 FIFO 都是弹头部

    def _sweep_expired(self):
        now = time.time()
        if now - self.last_sweep < self.policy.sweep_interval:
            return
        self.last_sweep = now
        dead = [k for k, v in self.entries.items() if v[1] is not None and now >= v[1]]
        for k in dead:
            del self.entries[k]


class FuguoCache:
    """全局缓存管理器（单例见文件底部 cache_manager）。"""

    def __init__(self):
        self._spaces: dict = {}
        self._create_lock = threading.Lock()

    def _space(self, cache_type: CacheType, namespace: str = "") -> _Space:
        name = f"{cache_type.value}:{namespace}" if namespace else cache_type.value
        space = self._spaces.get(name)
        if space is None:
            with self._create_lock:
                space = self._spaces.setdefault(name, _Space(policy_for(cache_type)))
        return space

    def set(
        self,
        cache_type: CacheType,
        key: str,
        value: Any,
        ttl: Optional[float] = None,
        namespace: str = "",
    ) -> None:
        """写入。ttl 显式传入时覆盖策略默认值。"""
        space = self._space(cache_type, namespace)
        effective_ttl = ttl if ttl is not None else space.policy.ttl
        expire_at = time.time() + effective_ttl if effective_ttl is not None else None
        with space.lock:
            space.entries.pop(key, None)
            space.entries[key] = (value, expire_at, time.time())
            space._evict_if_full()
            space._sweep_expired()

    def get(
        self, cache_type: CacheType, key: str, namespace: str = ""
    ) -> Optional[Any]:
        """读取；不存在或已过期返回 None。"""
        space = self._space(cache_type, namespace)
        with space.lock:
            if not space._alive(key):
                return None
            if space.policy.lru:
                space.entries.move_to_end(key)
            return space.entries[key][0]

    def delete(self, cache_type: CacheType, key: str, namespace: str = "") -> bool:
        space = self._space(cache_type, namespace)
        with space.lock:
            return space.entries.pop(key, None) is not None

    def clear(self, cache_type: CacheType, namespace: str = "") -> None:
        space = self._space(cache_type, namespace)
        with space.lock:
            space.entries.clear()


cache_manager = FuguoCache()
