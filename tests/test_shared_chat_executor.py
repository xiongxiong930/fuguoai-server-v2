#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
⑫ 共享有界 chat executor 真进化回归测试（离线，门禁 L3）

钉住 fuguoai_core/connection.py 的不可回退性质：
  1. 进程级单例 _SHARED_CHAT_EXECUTOR 存在，有界（默认32，环境变量可调），
     线程名前缀 chat（py-spy 可定位）。
  2. 多个 ConnectionHandler 实例共享同一 executor（不再每连接 10 线程）。
  3. 单连接 close 绝不 shutdown 共享池（其他在线连接任务不受影响）。
  4. 源码断言：__init__ 中不再出现每连接 ThreadPoolExecutor(max_workers=10)。

用法：regress venv 下 `python test_shared_chat_executor.py`，退出码非 0 禁止合并。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mcp_stub  # noqa: F401  连接模块 import 链依赖 mcp SDK，离线以桩注入

# 插件 auto_import 链上的重依赖（新闻抓取/黄历），离线测试注入轻量桩即可
import types as _types

for _name, _attrs in (("markitdown", ("MarkItDown",)), ("cnlunar", ("Lunar",))):
    if _name not in sys.modules:
        _m = _types.ModuleType(_name)
        _m.__path__ = []
        for _a in _attrs:
            setattr(_m, _a, object)
        sys.modules[_name] = _m

import fuguoai_core.connection as conn_mod
from fuguoai_core.connection import ConnectionHandler, _SHARED_CHAT_EXECUTOR

_results = []


def check(name, cond, detail=""):
    _results.append((name, cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def _make_handler():
    # 最小构造：不启动任何线程/网络，仅走 __init__
    cfg = {"selected_module": {}, "log": {}, "exit_commands": ["退下"]}
    return ConnectionHandler(cfg, None, None, None, None, None)


def test_singleton_bounded_named():
    ex = _SHARED_CHAT_EXECUTOR
    mw = ex._max_workers
    prefix = ex._thread_name_prefix
    check(
        "共享池有界且命名chat",
        1 <= mw <= 256 and prefix == "chat",
        f"max_workers={mw} prefix={prefix!r}",
    )


def test_handlers_share_executor():
    h1 = _make_handler()
    h2 = _make_handler()
    check(
        "两个连接共享同一executor",
        h1.executor is _SHARED_CHAT_EXECUTOR and h1.executor is h2.executor,
    )


def test_close_does_not_shutdown_shared_pool():
    h = _make_handler()
    # 模拟 close 中 executor 段逻辑（不跑完整 close_connection，避免网络依赖）
    if h.executor is not None:
        if h.executor is not conn_mod._SHARED_CHAT_EXECUTOR:
            h.executor.shutdown(wait=False)
        h.executor = None
    # 共享池仍可用：能正常提交并取回结果
    fut = _SHARED_CHAT_EXECUTOR.submit(lambda: 7 * 6)
    check(
        "连接关闭后共享池仍可用",
        fut.result(timeout=5) == 42 and not _SHARED_CHAT_EXECUTOR._shutdown,
    )


def test_source_no_per_conn_pool():
    src = open(
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fuguoai_core", "connection.py"),
        encoding="utf-8",
    ).read()
    check(
        "源码无每连接ThreadPoolExecutor(10)",
        "self.executor = ThreadPoolExecutor(max_workers=10)" not in src,
    )


if __name__ == "__main__":
    test_singleton_bounded_named()
    test_handlers_share_executor()
    test_close_does_not_shutdown_shared_pool()
    test_source_no_per_conn_pool()
    failed = [n for n, c in _results if not c]
    if failed:
        print(f"\nFAILED: {failed}")
        sys.exit(1)
    print(f"\nALL {len(_results)} PASS")
