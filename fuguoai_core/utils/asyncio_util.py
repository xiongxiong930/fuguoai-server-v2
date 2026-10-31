"""跨线程提交协程到事件循环并等待，带超时边界。

背景：本进程是单事件循环架构，音频处理 / 音频下发等 worker 线程通过
``asyncio.run_coroutine_threadsafe`` 把协程提交到主 loop 并 ``.result()`` 等待。
若 loop 因同步 CPU 热点（如 VAD / AEC 推理）卡顿，``.result()`` 无超时会导致
worker 线程无限挂起，进而引发全链路线程雪崩。此 helper 统一加超时边界：
loop 在 timeout 内未完成则抛 ``concurrent.futures.TimeoutError``，由调用方降级。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging

logger = logging.getLogger("fuguoai.asyncio_util")


def run_coroutine_threadsafe_with_timeout(
    coro,
    loop: asyncio.AbstractEventLoop,
    timeout: float,
    tag: str = "",
) -> object:
    """提交 coro 到 loop 并等待，最多 ``timeout`` 秒。

    - 正常完成：返回协程结果。
    - loop 在 ``timeout`` 内未完成（卡顿 / 挂死）：抛
      ``concurrent.futures.TimeoutError``。注意：超时仅停止等待，
      loop 上的协程仍会继续跑完（结果被丢弃），调用方应按"本次提交作废"降级，
      而非重试，避免重复副作用（如重复下发音频 / 重复处理音频帧）。
    """
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return fut.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        # tag 仅用于定位提交点；调用方负责面向运维的 WARNING，这里只留 debug 线索，
        # 避免同一次超时打两条日志。注意此处**不取消** fut：协程会继续跑完。
        logger.debug(
            "run_coroutine_threadsafe 等待超时 tag=%s timeout=%.1fs（协程未取消，仍在 loop 上执行）",
            tag or "-",
            timeout,
        )
        raise
