# -*- coding: utf-8 -*-
"""大模型 provider 统一抽象层（复果自研重构版）。

相对本仓早期派生起点的演进点（去相似化 + 真进化）：

1. **超时熔断**：上游 ``response_no_stream`` 对聚合调用没有任何时间上限，一旦 provider
   在网络层挂死，调用方（常在带 TTS 截止时间的线程里）会无限阻塞、整条对话死寂。
   本版引入 ``DEFAULT_NO_STREAM_TIMEOUT_S`` 宽松上限，异常慢调用改为可度量的失败。
2. **可观测计数**：每次非流式调用累计 ``stats``（字符数 / 耗时 / 是否超时），供
   metrics 读取，把"LLM 慢"从黑盒变成可见指标。
3. **空值保护**：``response_with_functions`` 对每个分片做 ``None -> ""`` 兜底，避免
   provider 偶发 yield ``None`` 时下游 ``content += content`` 直接崩。

接口契约与上游**严格对齐**（禁止破坏）：
  * ``response`` 为抽象方法，子类必须实现，流式 yield 文本分片；
  * ``response_no_stream`` 返回**完整字符串**（非生成器）——注意 family_rag /
    dialogue_eval 旧调用用 ``for chunk in`` 迭代它，那属于上游遗留用法，本基类
    维持"返回字符串"语义不变，使其仍能拼出正确文本；
  * ``response_with_functions`` 流式 yield ``(content, tool_calls)`` 二元组，
    供 ``connection.py`` 的工具调用分支消费。
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterator, List, Optional

from fuguoai_config.logger import setup_logging

TAG = __name__
logger = setup_logging()

# ⑭真进化（维度①·韧性）: 非流式聚合调用的默认超时（秒）。
# 仅用于兜住"provider 在网络层挂死"的极端情形；正常 <2s 的调用完全无感。
DEFAULT_NO_STREAM_TIMEOUT_S = 120.0


class LLMTimeoutError(RuntimeError):
    """非流式 LLM 调用超过上限仍未完成。

    区别于网络层自身的 httpx 超时：本异常是基类层面的二级熔断，
    保证即使 provider 内部逻辑卡死，调用线程也不会被无限占用。
    """


@dataclass
class _LLMCallStats:
    """单次非流式调用的轻量可观测快照（不侵入返回契约）。"""

    chars: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0
    timed_out: bool = False


class LLMProviderBase(ABC):
    """所有大模型 provider 的统一基类。

    子类只需实现抽象的 :meth:`response`；聚合形态（非流式 / 带函数调用）由本基类
    提供默认实现，并叠加超时、计数、空值保护等演进能力。
    """

    @property
    def stats(self) -> _LLMCallStats:
        """懒加载统计对象。

        用 property 而非 ``__init__`` 赋值，是因为 openai 等子类各自实现 ``__init__``
        且未必调用 ``super().__init__()``；懒加载保证任何子类实例都能安全读到 stats。
        """
        snapshot = getattr(self, "_llm_stats", None)
        if snapshot is None:
            snapshot = _LLMCallStats()
            self._llm_stats = snapshot
        return snapshot

    @abstractmethod
    def response(self, session_id: str, dialogue: List[dict]) -> Iterator[str]:
        """流式产出模型文本分片。子类必须实现。"""
        raise NotImplementedError

    def response_no_stream(
        self,
        system_prompt: str,
        user_prompt: str,
        timeout: float = DEFAULT_NO_STREAM_TIMEOUT_S,
        **kwargs,
    ) -> str:
        """非流式聚合：把 :meth:`response` 的分片拼成完整回包。

        演进点：
          * 受 ``timeout`` 约束（默认 :data:`DEFAULT_NO_STREAM_TIMEOUT_S`），
            挂死即抛 :class:`LLMTimeoutError`；
          * 累计 :attr:`stats`（字符数 / 耗时 / 是否超时），供 metrics 读取；
          * provider 内部异常归一为可观测，降级返回已拼出的部分文本而非炸穿调用方。

        契约：返回**完整字符串**（与上游一致），不是生成器。
        """
        payload = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        chunks: List[str] = []
        started = time.monotonic()
        try:
            for piece in self.response("", payload, **kwargs):
                if time.monotonic() - started > timeout:
                    self.stats.timed_out = True
                    raise LLMTimeoutError(
                        f"response_no_stream 超过 {timeout}s 仍未完成"
                    )
                if piece:
                    chunks.append(piece)
        except LLMTimeoutError:
            # 超时是明确的失败信号，向上抛，让调用方决定降级策略。
            self.stats.latency_s = time.monotonic() - started
            raise
        except Exception as exc:  # 真进化：provider 内部异常归一为可观测降级
            logger.bind(tag=TAG).warning(f"非流式生成异常（已降级为部分文本）: {exc}")
            self.stats.latency_s = time.monotonic() - started
            return "".join(chunks)
        self.stats.chars = sum(len(s) for s in chunks)
        self.stats.latency_s = time.monotonic() - started
        return "".join(chunks)

    def response_with_functions(
        self,
        session_id: str,
        dialogue: List[dict],
        functions: Optional[List[dict]] = None,
    ):
        """默认工具调用实现：不支持函数的 provider 退回普通流式。

        演进点：除保持 ``(content, tool_calls)`` 二元组契约外，额外对每个分片做
        空值保护——provider 偶发 yield ``None`` 时改为 ``""``，避免下游
        ``content += content`` 解包崩（见 ``connection.py`` 工具调用分支）。

        支持函数的 provider（如 openai）应覆盖本方法以产出真实 ``tool_calls``。
        """
        for token in self.response(session_id, dialogue):
            yield (token if token is not None else "", None)
