#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
④ TTS 常驻事件循环真进化回归测试（离线，门禁 L3）

钉住 fuguoai_core/providers/tts/base.py::_run_async 的不可回退性质：
  1. 语义与 asyncio.run 一致：返回值原样、异常原样抛出。
  2. 同线程多次合成复用同一 loop（loop 身份稳定 = 消除每段建/毁 loop 开销，
     并允许 loop 级状态跨段复用）。
  3. 不同线程各自持有独立 loop（无跨线程共享 loop 风险）。
  4. loop 被外部意外 close 后自愈重建。
  5. to_tts_stream 热路径已不再使用 asyncio.run（源码断言）。

用法：regress venv 下 `python test_tts_loop_reuse.py`，退出码非 0 禁止合并。
"""
import ast
import asyncio
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fuguoai_core.providers.tts import base as tts_base

_results = []


def check(name, cond, detail=""):
    _results.append((name, cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


async def _echo(v):
    await asyncio.sleep(0)
    return v


async def _boom():
    raise ValueError("boom")


def test_semantics():
    r = tts_base._run_on_thread_loop(_echo(b"audio"))
    check("语义-返回值原样", r == b"audio", f"got {r!r}")
    try:
        tts_base._run_on_thread_loop(_boom())
        check("语义-异常原样抛出", False, "未抛出")
    except ValueError as e:
        check("语义-异常原样抛出", str(e) == "boom")


def test_same_thread_loop_reuse():
    ids = set()

    async def _cap():
        return id(asyncio.get_running_loop())

    for _ in range(5):
        ids.add(tts_base._run_on_thread_loop(_cap()))
    check("同线程5次合成复用同一loop", len(ids) == 1, f"loop ids={ids}")


def test_cross_thread_isolation():
    async def _cap():
        return id(asyncio.get_running_loop())

    main_id = tts_base._run_on_thread_loop(_cap())
    other = {}

    def _worker():
        other["id"] = tts_base._run_on_thread_loop(_cap())
        other["id2"] = tts_base._run_on_thread_loop(_cap())

    t = threading.Thread(target=_worker)
    t.start()
    t.join()
    check(
        "跨线程loop隔离且线程内复用",
        other["id"] != main_id and other["id"] == other["id2"],
        f"main={main_id} other={other}",
    )


def test_self_heal_after_close():
    loop = tts_base._thread_loop.loop
    loop.close()
    r = tts_base._run_on_thread_loop(_echo(42))
    check("loop被close后自愈重建", r == 42)


def _docstring_line_numbers(src):
    """返回源码中所有 docstring 占用的行号集合（避免把文档里的字样误判为代码）。

    只按 `#` 前缀排除注释是不够的：模块/类/函数 docstring 里同样会以文字形式
    提到 `asyncio.run()`，那属于正常说明而非回退。故用 AST 精确定位 docstring。
    """
    tree = ast.parse(src)
    lines = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            end = first.end_lineno or first.lineno
            lines.update(range(first.lineno, end + 1))
    return lines


def test_hot_path_no_asyncio_run():
    src_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "fuguoai_core", "providers", "tts", "base.py",
    )
    src = open(src_path, encoding="utf-8").read()
    doc_lines = _docstring_line_numbers(src)
    # 只允许 docstring / 注释里出现 asyncio.run；代码行出现即回退
    code_hits = [
        ln for idx, ln in enumerate(src.splitlines(), start=1)
        if "asyncio.run(" in ln
        and idx not in doc_lines
        and not ln.lstrip().startswith("#")
    ]
    check("热路径无asyncio.run残留", not code_hits, f"hits={code_hits}")


if __name__ == "__main__":
    test_semantics()
    test_same_thread_loop_reuse()
    test_cross_thread_isolation()
    test_self_heal_after_close()
    test_hot_path_no_asyncio_run()
    failed = [n for n, c in _results if not c]
    if failed:
        print(f"\nFAILED: {failed}")
        sys.exit(1)
    print(f"\nALL {len(_results)} PASS")
