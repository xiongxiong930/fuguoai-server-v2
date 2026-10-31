"""VLLMProvider 真进化离线测试（phase5 / 5-B）

钉死: V1 响应方法必须是协程(异步), 且阻塞的视觉推理网络调用经 asyncio.to_thread
丢到线程池, 不冻结事件循环(原实现在 loop 上同步调用 chat.completions.create,
视觉推理期间所有设备WS/对讲/events 卡死)。

通过 sys.modules 注入 openai mock 后再导入模块, 避免 regress venv 拉真实 openai SDK。
"""
import asyncio
import sys
import time
import types
from unittest.mock import MagicMock

# —— 在导入 vllm 模块前注入 openai 替身 ——
_mock_openai = MagicMock()
sys.modules["openai"] = _mock_openai

import importlib
import inspect

import fuguoai_core.providers.vllm.openai as vllm_mod


def _make_create(return_content: str, block_s: float):
    """模拟一个会阻塞(在 worker 线程里)的 chat.completions.create。"""

    def _create(model=None, messages=None, stream=False):
        time.sleep(block_s)  # 阻塞的是 to_thread 的 worker 线程, 不是事件循环
        msg = types.SimpleNamespace(content=return_content)
        choice = types.SimpleNamespace(message=msg)
        return types.SimpleNamespace(choices=[choice])

    create = MagicMock(side_effect=_create)
    return create


def test_v1_response_is_async():
    create = _make_create("视觉结果", 0.0)
    _mock_openai.OpenAI.return_value.chat.completions.create = create
    vllm_mod.check_model_key = lambda *a, **k: None

    provider = vllm_mod.VLLMProvider(
        {"model_name": "m", "api_key": "k", "base_url": "http://x"}
    )
    assert inspect.iscoroutinefunction(
        provider.response
    ), "VLLMProvider.response 必须是 async def（事件循环解冻前提）"
    print("[V1] response 是协程函数 ✅")


def test_v2_nonblocking_and_wired():
    block_s = 0.2
    create = _make_create("视觉结果", block_s)
    _mock_openai.OpenAI.return_value.chat.completions.create = create
    vllm_mod.check_model_key = lambda *a, **k: None

    provider = vllm_mod.VLLMProvider(
        {"model_name": "m", "api_key": "k", "base_url": "http://x"}
    )

    counter = [0]

    async def _watchdog():
        # 每 10ms 递增; 若 loop 被冻结, 计数会停在很低的值
        for _ in range(20):
            await asyncio.sleep(0.01)
            counter[0] += 1

    async def _main():
        res, _ = await asyncio.gather(
            provider.response("这是图?", "BASE64IMG"),
            _watchdog(),
        )
        return res

    res = asyncio.run(_main())
    assert res == "视觉结果", f"返回内容错误: {res!r}"
    # 0.2s 阻塞期间, loop 应持续调度 watchdog(每 10ms 一次) → 计数应远大于 1
    assert counter[0] >= 5, (
        f"视觉推理期间事件循环被冻结! watchdog 计数仅 {counter[0]}"
    )
    # 调用确实发生且参数正确
    create.assert_called_once()
    kwargs = create.call_args.kwargs
    assert kwargs.get("model") == "m", f"model 参数错误: {kwargs}"
    assert isinstance(kwargs.get("messages"), list) and kwargs["messages"], (
        f"messages 参数错误: {kwargs}"
    )
    print(
        f"[V2] 阻塞 {block_s}s 期间 loop 仍响应(watchdog={counter[0]}) + create 调用正确 ✅"
    )


if __name__ == "__main__":
    test_v1_response_is_async()
    test_v2_nonblocking_and_wired()
    print("\nALL VLLM EVOLUTION TESTS PASS ✅")
