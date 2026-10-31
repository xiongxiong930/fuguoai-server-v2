# -*- coding: utf-8 -*-
"""角色对话流式链路自进化测试（2026-10-16）——锁两处修复：

① 历史进 dialogue：`_get_history` 的滚动窗口必须拼进 LLM messages。
   原 bug：history 取了但没拼（dialogue 只有 system+user），多轮上下文全丢。
② 同步 LLM 流下沉：`_llm_pieces_async` 用后台线程 + 队列桥接同步生成器。
   本服务为单事件循环架构，协程里直接迭代同步流会卡死同进程 WS/其他 HTTP；
   桥接后消费期间事件循环必须保持响应（ticker 协程持续推进）。
"""

import asyncio
import json as _json
import threading
import time

import pytest

# 注意：必须在模块顶层 import（测试函数体内 import 会撞上模块级
# setup_logging→asyncio.run(load_config)，在已运行的事件循环里直接炸）。
from fuguoai_core.api import character_routes as cr
from fuguoai_core.api.character_routes import (
    _build_dialogue,
    _llm_pieces_async,
)
from fuguoai_core.utils.character_store import (
    CHANNEL_APP,
    CharacterSession,
    DeliveryTarget,
)


def _make_llm(pieces, delay=0.0, seen=None):
    class _LLM:
        def response(self, session_id, dialogue, **kwargs):
            if seen is not None:
                seen.append(dialogue)
            for p in pieces:
                if delay:
                    time.sleep(delay)  # 模拟同步流阻塞读（真实 openai 流每 chunk 都是阻塞 socket 读）
                yield p

    return _LLM()


@pytest.mark.asyncio
async def test_history_goes_into_dialogue():
    """滚动历史拼进 dialogue；只认 user/assistant，脏数据/空 content 丢弃。"""
    history = [
        {"role": "user", "content": "第一句"},
        {"role": "assistant", "content": "第一次回"},
        {"role": "system", "content": "脏数据-应被丢弃"},
        {"role": "user", "content": ""},
    ]
    d = _build_dialogue("SP", history, "本轮")
    assert d[0] == {"role": "system", "content": "SP"}
    assert d[-1] == {"role": "user", "content": "本轮"}
    assert {"role": "user", "content": "第一句"} in d
    assert {"role": "assistant", "content": "第一次回"} in d
    assert len(d) == 4  # system + 2 条有效历史 + user


@pytest.mark.asyncio
async def test_stream_bridge_yields_all_pieces():
    llm = _make_llm(["你", "好", "呀"])
    got = [p async for p in _llm_pieces_async(llm, [{"role": "user", "content": "hi"}])]
    assert got == ["你", "好", "呀"]


@pytest.mark.asyncio
async def test_stream_bridge_does_not_block_event_loop():
    """producer 每块 sleep 50ms；若协程直接迭代同步流，ticker 会整段卡住。"""
    llm = _make_llm(["a", "b", "c"], delay=0.05)
    ticks = []

    async def ticker():
        for _ in range(10):
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    async def consume():
        return [p async for p in _llm_pieces_async(
            llm, [{"role": "user", "content": "hi"}])]

    pieces, _ = await asyncio.gather(consume(), ticker())
    assert pieces == ["a", "b", "c"]
    # 桥接生效：消费全程 ticker 持续推进，无 50ms 级卡顿
    assert len(ticks) == 10
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert max(gaps) < 0.04


@pytest.mark.asyncio
async def test_stream_bridge_propagates_exception():
    class _Bad:
        def response(self, session_id, dialogue, **kwargs):
            yield "半个"
            raise RuntimeError("provider 炸了")

    with pytest.raises(RuntimeError, match="provider 炸了"):
        async for _ in _llm_pieces_async(_Bad(), [{"role": "user", "content": "x"}]):
            pass


@pytest.mark.asyncio
async def test_stream_bridge_stop_event_halts_producer():
    """客户端断开场景：stop_evt 预置后 producer 不得产出任何块。"""
    produced = []

    class _Endless:
        def response(self, session_id, dialogue, **kwargs):
            i = 0
            while True:
                produced.append(i)
                i += 1
                time.sleep(0.01)
                yield str(i)

    stop = threading.Event()
    stop.set()
    got = [p async for p in _llm_pieces_async(_Endless(), [], stop_evt=stop)]
    assert got == []        # 未入队任何块（stop 检查先于 put）
    assert produced == [0]  # generator 首次 next 只跑到 yield 前即被 break


# ---------------------------------------------------------------------------
# SSE 全链路：handle_message(stream=True) 带历史 + delta/done 帧
# ---------------------------------------------------------------------------
class _FakeStreamResp:
    def __init__(self):
        self.headers = {}
        self.writes = []
        self.eof = False

    def enable_compression(self, flag):
        pass

    async def prepare(self, request):
        return self

    async def write(self, data):
        self.writes.append(data)

    async def write_eof(self):
        self.eof = True


class _Req:
    def __init__(self, body=None, **match_info):
        self.match_info = match_info
        self._body = body or {}

    async def json(self):
        return self._body


@pytest.mark.asyncio
async def test_sse_message_includes_history_and_frames(monkeypatch):
    from fuguoai_core.utils import character_memory

    seen = []

    class _LLM:
        def response(self, session_id, dialogue, **kwargs):
            seen.append(dialogue)
            yield "回复:"
            yield "本轮"

    router = type("R", (), {})()
    router.profiles = type("P", (), {"get": staticmethod(lambda cid: type(
        "X", (), {"character_id": cid})())})()
    router.sessions = type("S", (), {"get": staticmethod(lambda uid: CharacterSession(
        uid=uid, character_id="chuxue-001", channel=CHANNEL_APP))})()
    router.build_system_prompt = lambda uid: "SP-test"
    router.route_response = lambda uid, text: DeliveryTarget(
        target=CHANNEL_APP, character_id="chuxue-001", text=text, voice="v1")

    monkeypatch.setattr(cr, "_auth", lambda request: _noop_coro(("user-1", None)))
    monkeypatch.setattr(cr, "get_character_router", lambda: router)
    monkeypatch.setattr(cr, "get_card_registry", lambda: type("G", (), {
        "owns": staticmethod(lambda uid, cid: True)})())
    monkeypatch.setattr(cr, "_get_history", lambda uid, cid: [
        {"role": "user", "content": "上一句"},
        {"role": "assistant", "content": "上一回"},
    ])
    pushed = []
    monkeypatch.setattr(cr, "_push_history",
                        lambda uid, cid, role, content: pushed.append((role, content)))
    monkeypatch.setattr(cr, "_get_llm", lambda: _LLM())
    from fuguoai_core.utils import character_memory
    monkeypatch.setattr(character_memory, "tier_of", lambda uid, cid: character_memory.TIER_SHORT)
    monkeypatch.setattr(character_memory, "apply_to_system_prompt",
                        lambda sp, uid, cid, query="": (sp, ""))
    monkeypatch.setattr(character_memory, "extract_async", lambda *a, **k: None)
    monkeypatch.setattr(cr.web, "StreamResponse", _FakeStreamResp)

    resp = await cr.handle_message(
        _Req(body={"content": "本轮", "stream": True}, character_id="chuxue-001"))

    # ① 历史确实拼进了 LLM dialogue
    assert seen, "LLM 未被调用"
    d = seen[0]
    assert d[0]["role"] == "system"
    assert {"role": "user", "content": "上一句"} in d
    assert {"role": "assistant", "content": "上一回"} in d
    assert d[-1] == {"role": "user", "content": "本轮"}

    # ② SSE 帧：delta 逐块 + done 汇总 + 历史落库
    frames = [_json.loads(w.decode("utf-8")[len("data: "):].strip())
              for w in resp.writes if w.startswith(b"data: ")]
    deltas = [f["delta"] for f in frames if "delta" in f]
    assert deltas == ["回复:", "本轮"]
    done = [f for f in frames if f.get("done")]
    assert done and done[0]["reply"] == "回复:本轮"
    assert resp.eof is True
    assert ("user", "本轮") in pushed and ("assistant", "回复:本轮") in pushed


async def _noop_coro(value):
    return value
