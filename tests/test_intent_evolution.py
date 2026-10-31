# -*- coding: utf-8 -*-
"""意图识别演进三件套离线回归（2026-07-29）

锁定三项自研演进不被回退：
  E1  LLM 调用下线程池 —— 意图识别期间事件循环必须保持响应
      （单 loop 架构下裸阻塞会冻结全进程 WS 收发，含对讲 relay 20ms 节奏）
  E2  确定性快路径 —— 时间/日期/农历高频查询零 LLM 往返
  E3  超时熔断 —— 慢 LLM 降级 continue_chat，不拖垮整轮对话

无网络依赖，供 tools/regression_gate.py L3 调用。
"""

import asyncio
import sys
import time
import uuid

from fuguoai_core.providers.intent.intent_llm import intent_llm as mod
from fuguoai_core.providers.intent.intent_llm.intent_llm import (
    IntentProvider,
    _CONTEXT_ANSWER,
    _FALLBACK,
    _fast_context_route,
)

PASS = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ✓ {name}")


# ── 测试桩 ───────────────────────────────────────────────────────────────

class _Msg:
    def __init__(self, role, content):
        self.role, self.content = role, content


class _Dialogue:
    def __init__(self):
        self.dialogue = [_Msg("user", "你好"), _Msg("assistant", "你好呀")]


class _FuncHandler:
    def get_functions(self):
        return []


class _SlowLLM:
    """同步阻塞 LLM 桩：睡 sleep_s 秒后返回合法意图 JSON。"""

    model_name = "stub-slow-llm"

    def __init__(self, sleep_s):
        self.sleep_s = sleep_s

    def response_no_stream(self, system_prompt, user_prompt, **kw):
        time.sleep(self.sleep_s)
        return '{"function_call": {"name": "self_test_tool"}}'


class _Conn:
    def __init__(self):
        self.device_id = "evo_" + uuid.uuid4().hex
        self.config = {"plugins": {}}
        self.dialogue = _Dialogue()
        self.func_handler = _FuncHandler()


def _make_provider(llm, timeout=8.0):
    p = IntentProvider({"llm_timeout": timeout})
    p.set_llm(llm)
    return p


# ── E2 快路径 ────────────────────────────────────────────────────────────

def test_fast_path():
    print("[E2] 确定性快路径")
    # 命中：标点/空白不敏感
    for q in ("现在几点了？", "今天星期几", "今天 星期几", "现在几点", "今天农历几号！"):
        assert _fast_context_route(q) == _CONTEXT_ANSWER, f"应命中: {q}"
    ok("高频基础查询精确命中 result_for_context")
    # 不命中：含歧义/疑问语境/无关输入一律走 LLM
    for q in ("怎么退出", "现在几点开会", "今天星期几出生的人怎么样", "帮我开灯", "", None):
        assert _fast_context_route(q) is None, f"不应命中: {q}"
    ok("歧义输入不误伤（照走 LLM）")

    async def run():
        conn = _Conn()
        p = _make_provider(_SlowLLM(5.0))  # 若走 LLM 会睡 5s
        t0 = time.perf_counter()
        r = await p.detect_intent(conn, conn.dialogue.dialogue, "现在几点了？")
        return r, time.perf_counter() - t0

    r, dt = asyncio.run(run())
    assert r == _CONTEXT_ANSWER and dt < 0.1, f"快路径应毫秒级返回, 实际 {dt:.3f}s"
    ok(f"detect_intent 快路径 {dt * 1000:.1f}ms 返回（未触发 LLM）")


# ── E1 事件循环响应性 ────────────────────────────────────────────────────

def test_loop_responsiveness():
    print("[E1] LLM 调用下线程池，事件循环不冻结")

    async def run():
        conn = _Conn()
        p = _make_provider(_SlowLLM(0.6))
        gaps, stop = [], [False]

        async def heartbeat():
            last = time.perf_counter()
            while not stop[0]:
                await asyncio.sleep(0.02)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        hb = asyncio.create_task(heartbeat())
        r = await p.detect_intent(conn, conn.dialogue.dialogue, "帮我开一下客厅的灯")
        stop[0] = True
        await hb
        return r, max(gaps)

    r, worst_gap = asyncio.run(run())
    assert "self_test_tool" in r, f"意图结果异常: {r}"
    # 裸阻塞时心跳最大间隔≈0.6s；下线程池后应保持 20ms 节奏（留 10 倍余量）
    assert worst_gap < 0.25, (
        f"事件循环被冻结: 心跳最大间隔 {worst_gap * 1000:.0f}ms ≥ 250ms"
        "（LLM 调用疑似回退为裸同步）"
    )
    ok(f"0.6s LLM 调用期间心跳最大间隔 {worst_gap * 1000:.0f}ms（loop 全程响应）")


# ── E3 超时熔断 ──────────────────────────────────────────────────────────

def test_timeout_fuse():
    print("[E3] LLM 超时熔断降级")

    async def run():
        conn = _Conn()
        p = _make_provider(_SlowLLM(0.9), timeout=0.3)
        t0 = time.perf_counter()
        r = await p.detect_intent(conn, conn.dialogue.dialogue, "把空调调到26度")
        return r, time.perf_counter() - t0

    r, dt = asyncio.run(run())
    assert r == _FALLBACK, f"超时应降级 continue_chat, 实际: {r}"
    assert dt < 0.8, f"熔断应在 timeout 附近返回, 实际 {dt:.3f}s"
    ok(f"0.3s 熔断生效，{dt:.3f}s 降级 continue_chat")
    # 配置解析健壮性
    assert IntentProvider({"llm_timeout": "abc"}).llm_timeout == IntentProvider._DEFAULT_LLM_TIMEOUT
    assert IntentProvider({}).llm_timeout == IntentProvider._DEFAULT_LLM_TIMEOUT
    assert IntentProvider({"llm_timeout": 3}).llm_timeout == 3.0
    ok("llm_timeout 配置解析（非法值回落默认）")


if __name__ == "__main__":
    # 隔离音乐目录扫描（无关被测路径）
    mod.initialize_music_handler = lambda conn: {"music_file_names": ""}
    test_fast_path()
    test_loop_responsiveness()
    test_timeout_fuse()
    print(f"\nALL {PASS} CHECKS PASS ✓ (intent evolution E1/E2/E3)")
    sys.exit(0)
