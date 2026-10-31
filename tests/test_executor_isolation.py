"""离线测试 ASR / 意图识别的专属有界线程池隔离（Tier A-⑥）。

核心断言：ASR 与意图 LLM 的网络调用运行在**独立命名**的线程池
（thread_name_prefix="asr" / "intent"），而非 loop 默认池。这样单个慢
ASR / 慢意图请求占满专属池时，不会拖垮其他设备的意图识别与下沉任务
（消除跨设备干扰）。
"""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor


class TestExecutorIsolation(unittest.TestCase):
    def test_asr_executor_isolated(self):
        from fuguoai_core.providers.asr.tencent import _ASR_EXECUTOR

        self.assertIsInstance(_ASR_EXECUTOR, ThreadPoolExecutor)
        name = _ASR_EXECUTOR.submit(
            lambda: threading.current_thread().name
        ).result(timeout=5)
        self.assertTrue(name.startswith("asr"), f"ASR 未跑在专属池: {name}")

    def test_intent_executor_isolated(self):
        from fuguoai_core.providers.intent.intent_llm.intent_llm import (
            _INTENT_EXECUTOR,
        )

        self.assertIsInstance(_INTENT_EXECUTOR, ThreadPoolExecutor)
        name = _INTENT_EXECUTOR.submit(
            lambda: threading.current_thread().name
        ).result(timeout=5)
        self.assertTrue(name.startswith("intent"), f"意图未跑在专属池: {name}")


if __name__ == "__main__":
    unittest.main()
