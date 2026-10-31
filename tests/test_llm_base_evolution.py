# -*- coding: utf-8 -*-
"""锁定 providers/llm/base.py 的「去相似化 + 真进化」契约。

验证项：
  1. response_no_stream 返回完整字符串（契约：非生成器）。
  2. 超时熔断：provider 拖慢时分片超上限即抛 LLMTimeoutError。
  3. 空值保护：response_with_functions 把 None 分片兜底为 ""，保持 (content, tc) 二元组。
  4. 可观测：stats 累计字符数 / 耗时。
  5. 异常归一：provider 内部异常时降级返回已拼部分文本，不炸穿调用方。
"""
import sys
import time

from fuguoai_core.providers.llm.base import (
    LLMProviderBase,
    LLMTimeoutError,
    DEFAULT_NO_STREAM_TIMEOUT_S,
)

_FAILS = []


def check(cond, msg):
    if cond:
        print(f"  [OK] {msg}")
    else:
        print(f"  [FAIL] {msg}")
        _FAILS.append(msg)


class _EchoProvider(LLMProviderBase):
    """正常 provider：把传入文本逐字 yield。"""

    def __init__(self, text="你好世界"):
        self._text = text

    def response(self, session_id, dialogue):
        for ch in self._text:
            yield ch


class _SlowProvider(LLMProviderBase):
    """慢 provider：先吐一个分片，睡够时间后再吐第二个，触发超时。"""

    def response(self, session_id, dialogue):
        yield "a"
        time.sleep(0.12)
        yield "b"


class _BoomProvider(LLMProviderBase):
    """异常 provider：吐一个分片后抛错。"""

    def response(self, session_id, dialogue):
        yield "x"
        raise RuntimeError("模拟 provider 内部故障")


class _NoneProvider(LLMProviderBase):
    """偶发 yield None 的 provider，验证空值保护。"""

    def response(self, session_id, dialogue):
        yield "hi"
        yield None
        yield "bye"


def main():
    print("=== test_llm_base_evolution ===")

    # 1. 聚合返回完整字符串
    p = _EchoProvider("你好世界")
    out = p.response_no_stream("sys", "usr")
    check(isinstance(out, str) and out == "你好世界",
          f"response_no_stream 返回完整字符串: {out!r}")
    check(p.stats.chars == 4, f"stats.chars 累计正确: {p.stats.chars}")
    check(p.stats.latency_s >= 0 and not p.stats.timed_out,
          "stats 耗时记录且未超时")

    # 2. 超时熔断（小超时）
    slow = _SlowProvider()
    t0 = time.monotonic()
    raised = False
    try:
        slow.response_no_stream("sys", "usr", timeout=0.03)
    except LLMTimeoutError:
        raised = True
    elapsed = time.monotonic() - t0
    check(raised, f"慢 provider 触发 LLMTimeoutError (elapsed={elapsed:.3f}s)")
    check(slow.stats.timed_out, "stats.timed_out 标记为 True")

    # 3. 空值保护 + 二元组契约
    none_p = _NoneProvider()
    pairs = list(none_p.response_with_functions("sid", []))
    check(all(isinstance(pr, tuple) and len(pr) == 2 for pr in pairs),
          "response_with_functions 产出 (content, tc) 二元组")
    contents = [c for c, _ in pairs]
    check(contents == ["hi", "", "bye"],
          f"None 分片被兜底为 '' : {contents}")

    # 4. 异常归一降级（不炸穿）
    boom = _BoomProvider()
    partial = boom.response_no_stream("sys", "usr")
    check(partial == "x", f"provider 异常时降级返回已拼部分: {partial!r}")

    # 5. 默认超时常量合理性
    check(DEFAULT_NO_STREAM_TIMEOUT_S > 0, "默认超时上限为正值")

    if _FAILS:
        print(f"\n结果: {len(_FAILS)} 项 FAIL ❌")
        sys.exit(1)
    print("\n结果: ALL PASS ✅")


if __name__ == "__main__":
    main()
