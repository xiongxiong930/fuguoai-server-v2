"""离线测试 run_coroutine_threadsafe_with_timeout 的反雪崩属性。

核心断言：当事件循环因同步 CPU 热点(VAD/AEC 推理)卡顿、协程无法在 timeout
内完成时，worker 线程的等待是**有界**的（抛 TimeoutError 而非无限挂起），
从而消除"单点 loop 卡顿 -> 全链路线程级联挂起"的雪崩模式。
"""
import asyncio
import concurrent.futures
import threading
import time
import unittest

from fuguoai_core.utils.asyncio_util import run_coroutine_threadsafe_with_timeout


def _run_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


class TestAsyncioUtilTimeout(unittest.TestCase):
    def test_returns_when_completes_in_time(self):
        loop = asyncio.new_event_loop()
        t = threading.Thread(target=_run_loop, args=(loop,), daemon=True)
        t.start()
        try:
            async def fast():
                await asyncio.sleep(0.05)
                return "ok"

            res = run_coroutine_threadsafe_with_timeout(fast(), loop, timeout=2)
            self.assertEqual(res, "ok")
        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=2)

    def test_timeout_when_coro_hangs(self):
        """loop 上的协程长期挂起 -> 等待有界、不会无限卡住 worker 线程。"""
        loop = asyncio.new_event_loop()
        t = threading.Thread(target=_run_loop, args=(loop,), daemon=True)
        t.start()
        try:
            async def hang():
                await asyncio.sleep(5)
                return "never"

            start = time.monotonic()
            with self.assertRaises(concurrent.futures.TimeoutError):
                run_coroutine_threadsafe_with_timeout(hang(), loop, timeout=0.3)
            elapsed = time.monotonic() - start
            # 关键：约 0.3s 超时返回，而非等满 5s —— 证明等待有界（反雪崩）
            self.assertLess(elapsed, 2.0)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=2)

    def test_timeout_is_exception_subclass(self):
        # 调用方用 `except Exception` 也能兜住，不会漏成未捕获异常
        self.assertTrue(issubclass(concurrent.futures.TimeoutError, Exception))


if __name__ == "__main__":
    unittest.main()
