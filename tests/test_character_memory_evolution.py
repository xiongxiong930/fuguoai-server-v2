# -*- coding: utf-8 -*-
"""天使心「心记忆」分层「自进化」测试（纯单元，不依赖运行服务器）。

覆盖 fuguoai_core/utils/character_memory.py（此前只有被当桩 monkeypatch 的间接
引用，分层逻辑本身零直接覆盖——而它是「买卡=长期记忆」这个核心卖点的落点）：

  ① tier_of 分层判定：持卡=long / 别人持卡=short / 无 character_id=short /
     注册表异常保守降级 short（绝不因记忆层拖垮对话）
  ② retrieve / build_memory_block / apply_to_system_prompt：
     - short 层一律空（不读不注入——升级欲是刻意设计，不是缺陷）
     - long 层关键词命中优先、无 query 时时间倒序、limit 生效
     - store 读取异常全吞
  ③ extract_sync：short 层不调 LLM 不落库 / llm_getter 缺失或 LLM 异常全吞 /
     JSON 数组解析 + 去重 + 长度过滤(1..60) + 单轮上限 3 条
  ④ extract_async：short 层不排任务 / long 层提交后台任务 / 任务异常全吞
  ⑤ clear：清空返回条数 / 异常全吞

真存储（MemoryStore / CardRegistry 均落 tmp 盘），不打桩存储层——
记忆分层是否正确，只有跑真存储才算数。
"""

import json

import pytest

from fuguoai_core.utils import character_memory as cm
from fuguoai_core.utils.angel_heart_store import MemoryStore


# ───────────────────────── 夹具 ─────────────────────────

def _write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


@pytest.fixture
def mem_env(tmp_path, monkeypatch):
    """真 MemoryStore + 真 CardRegistry（tmp 落盘）。

    返回 (store, mem_path, card_path)。card_path 可随时改文件内容，
    CardRegistry 每次调用都重新读盘 → 等效"重启后仍在"。
    """
    from fuguoai_core.utils.card_registry import CardRegistry

    mem_path = str(tmp_path / "memories.json")
    card_path = str(tmp_path / "card_registry.json")
    _write_json(mem_path, {})
    _write_json(card_path, {"cards": {}})

    store = MemoryStore(mem_path)
    registry = CardRegistry(path=card_path)
    monkeypatch.setattr(
        "fuguoai_core.utils.angel_heart_store.get_memory_store", lambda: store)
    monkeypatch.setattr(
        "fuguoai_core.utils.card_registry.get_card_registry", lambda: registry)
    return store, mem_path, card_path


def _seed_memories(mem_path, facts):
    """直接写记忆文件（可指定 created_at，绕开 _now() 同秒碰撞）。"""
    _write_json(mem_path, {"user-1": {"char-1": facts}})


def _seed_cards(card_path, cards):
    _write_json(card_path, {"cards": cards})


class _LLM:
    """记录调用的假 LLM：response_no_stream 返回预设文本。"""

    def __init__(self, reply):
        self._reply = reply
        self.calls = []

    def response_no_stream(self, prompt, system=""):
        self.calls.append((prompt, system))
        return self._reply


# ───────────────────────── ① tier_of 分层判定 ─────────────────────────

def test_tier_long_when_owned(mem_env):
    _, _, card_path = mem_env
    _seed_cards(card_path, {"CARD1": {"character_id": "char-1", "owner_uid": "user-1"}})
    assert cm.tier_of("user-1", "char-1") == cm.TIER_LONG


def test_tier_short_when_owned_by_other(mem_env):
    _, _, card_path = mem_env
    _seed_cards(card_path, {"CARD1": {"character_id": "char-1", "owner_uid": "someone-else"}})
    assert cm.tier_of("user-1", "char-1") == cm.TIER_SHORT


def test_tier_short_without_character_id(mem_env):
    # 无 character_id → owns 恒 False → short（不因空 id 误判长期）
    assert cm.tier_of("user-1", "") == cm.TIER_SHORT


def test_tier_short_on_registry_exception(mem_env, monkeypatch):
    """注册表异常（文件损坏/IO 错误）→ 保守降级短期，绝不拖垮对话。"""
    import fuguoai_core.utils.card_registry as cr_mod

    class _Boom:
        def owns(self, *a, **k):
            raise RuntimeError("registry down")

    monkeypatch.setattr(cr_mod, "get_card_registry", lambda: _Boom())
    assert cm.tier_of("user-1", "char-1") == cm.TIER_SHORT


# ───────────────────────── ② retrieve / block / apply ─────────────────────────

def test_short_tier_never_reads_or_injects(mem_env):
    """short 层：store 里明明有数据，也不读、不注入（升级欲的来源）。"""
    store, mem_path, _ = mem_env
    _seed_memories(mem_path, [
        {"id": "m1", "character_id": "char-1", "fact": "用户住在成都", "created_at": 100},
    ])
    assert cm.retrieve("user-1", "char-1", query="用户住在成都") == []
    assert cm.build_memory_block("user-1", "char-1", query="用户住在成都") == ""
    prompt, block = cm.apply_to_system_prompt("BASE", "user-1", "char-1")
    assert prompt == "BASE" and block == ""


def test_long_tier_keyword_hit_priority(mem_env):
    _, mem_path, card_path = mem_env
    _seed_cards(card_path, {"CARD1": {"character_id": "char-1", "owner_uid": "user-1"}})
    _seed_memories(mem_path, [
        {"id": "m1", "character_id": "char-1", "fact": "用户养了一只猫", "created_at": 300},
        {"id": "m2", "character_id": "char-1", "fact": "用户住在成都", "created_at": 100},
        {"id": "m3", "character_id": "char-1", "fact": "用户怕吃辣", "created_at": 200},
    ])
    # query 完整命中"用户住在成都"（tokens 是 2+ 连续中文的最大串）→ 排最前
    got = cm.retrieve("user-1", "char-1", query="用户住在成都")
    assert got[0] == "用户住在成都"
    assert set(got) == {"用户养了一只猫", "用户住在成都", "用户怕吃辣"}


def test_long_tier_time_desc_when_no_query(mem_env):
    _, mem_path, card_path = mem_env
    _seed_cards(card_path, {"CARD1": {"character_id": "char-1", "owner_uid": "user-1"}})
    _seed_memories(mem_path, [
        {"id": "m1", "character_id": "char-1", "fact": "旧事实", "created_at": 100},
        {"id": "m2", "character_id": "char-1", "fact": "新事实", "created_at": 200},
    ])
    assert cm.retrieve("user-1", "char-1", query="") == ["新事实", "旧事实"]


def test_long_tier_limit_default_eight(mem_env):
    _, mem_path, card_path = mem_env
    _seed_cards(card_path, {"CARD1": {"character_id": "char-1", "owner_uid": "user-1"}})
    _seed_memories(mem_path, [
        {"id": f"m{i}", "character_id": "char-1", "fact": f"事实{i}", "created_at": i}
        for i in range(10)
    ])
    assert len(cm.retrieve("user-1", "char-1")) == cm._MEMORY_INJECT_LIMIT == 8


def test_retrieve_swallows_store_error(mem_env, monkeypatch):
    store, _, card_path = mem_env
    _seed_cards(card_path, {"CARD1": {"character_id": "char-1", "owner_uid": "user-1"}})

    def _boom(uid, cid):
        raise RuntimeError("store down")

    monkeypatch.setattr(store, "list", _boom)
    assert cm.retrieve("user-1", "char-1") == []


def test_build_memory_block_format(mem_env):
    _, mem_path, card_path = mem_env
    _seed_cards(card_path, {"CARD1": {"character_id": "char-1", "owner_uid": "user-1"}})
    _seed_memories(mem_path, [
        {"id": "m1", "character_id": "char-1", "fact": "用户住在成都", "created_at": 100},
    ])
    block = cm.build_memory_block("user-1", "char-1", query="")
    assert block.startswith("<memory>") and block.endswith("</memory>")
    assert "- 用户住在成都" in block


def test_build_memory_block_empty_when_no_facts(mem_env):
    _, _, card_path = mem_env
    _seed_cards(card_path, {"CARD1": {"character_id": "char-1", "owner_uid": "user-1"}})
    assert cm.build_memory_block("user-1", "char-1", query="") == ""


def test_apply_to_system_prompt_appends_block(mem_env):
    _, mem_path, card_path = mem_env
    _seed_cards(card_path, {"CARD1": {"character_id": "char-1", "owner_uid": "user-1"}})
    _seed_memories(mem_path, [
        {"id": "m1", "character_id": "char-1", "fact": "用户住在成都", "created_at": 100},
    ])
    prompt, block = cm.apply_to_system_prompt("BASE-PROMPT", "user-1", "char-1", query="")
    assert prompt == "BASE-PROMPT\n\n" + block
    assert "<memory>" in block


# ───────────────────────── ③ extract_sync 抽取落库 ─────────────────────────

def test_extract_sync_short_tier_no_llm_no_write(mem_env):
    """short 层：不调 LLM、不写任何长期记忆、返回 0。"""
    store, _, _ = mem_env
    llm = _LLM('["不该写"]')
    n = cm.extract_sync("user-1", "char-1", "你好", "你好呀", llm_getter=lambda: llm)
    assert n == 0
    assert llm.calls == []
    assert store.list("user-1", "char-1") == []


def test_extract_sync_no_llm_getter(mem_env):
    _, _, card_path = mem_env
    _seed_cards(card_path, {"CARD1": {"character_id": "char-1", "owner_uid": "user-1"}})
    assert cm.extract_sync("user-1", "char-1", "你好", "你好呀", llm_getter=None) == 0


def test_extract_sync_happy_writes_and_dedups(mem_env):
    store, _, card_path = mem_env
    _seed_cards(card_path, {"CARD1": {"character_id": "char-1", "owner_uid": "user-1"}})
    llm = _LLM('["用户喜欢咖啡", "用户住在成都"]')

    n = cm.extract_sync("user-1", "char-1", "我喜欢咖啡", "记住了", llm_getter=lambda: llm)
    assert n == 2
    assert len(store.list("user-1", "char-1")) == 2

    # 再次抽取同样的事实 → 去重，一条不写
    llm2 = _LLM('["用户喜欢咖啡", "用户住在成都"]')
    n2 = cm.extract_sync("user-1", "char-1", "我喜欢咖啡", "记住了", llm_getter=lambda: llm2)
    assert n2 == 0
    assert len(store.list("user-1", "char-1")) == 2


def test_extract_sync_llm_exception_swallowed(mem_env):
    store, _, card_path = mem_env
    _seed_cards(card_path, {"CARD1": {"character_id": "char-1", "owner_uid": "user-1"}})

    class _BoomLLM:
        def response_no_stream(self, prompt, system=""):
            raise RuntimeError("llm down")

    n = cm.extract_sync("user-1", "char-1", "你好", "你好呀", llm_getter=lambda: _BoomLLM())
    assert n == 0
    assert store.list("user-1", "char-1") == []


def test_extract_sync_non_json_reply(mem_env):
    store, _, card_path = mem_env
    _seed_cards(card_path, {"CARD1": {"character_id": "char-1", "owner_uid": "user-1"}})
    llm = _LLM("我觉得没有什么值得记住的。")
    assert cm.extract_sync("user-1", "char-1", "你好", "你好呀", llm_getter=lambda: llm) == 0
    assert store.list("user-1", "char-1") == []


def test_extract_sync_empty_array(mem_env):
    _, _, card_path = mem_env
    _seed_cards(card_path, {"CARD1": {"character_id": "char-1", "owner_uid": "user-1"}})
    llm = _LLM("[]")
    assert cm.extract_sync("user-1", "char-1", "你好", "你好呀", llm_getter=lambda: llm) == 0


def test_extract_sync_drops_overlong_and_empty_items(mem_env):
    """长度过滤：空串丢、>60 字丢、1..60 字留。"""
    store, _, card_path = mem_env
    _seed_cards(card_path, {"CARD1": {"character_id": "char-1", "owner_uid": "user-1"}})
    ok60 = "甲" * 60
    too_long = "甲" * 61
    reply = json.dumps(["", too_long, ok60], ensure_ascii=False)
    llm = _LLM(reply)
    n = cm.extract_sync("user-1", "char-1", "你好", "你好呀", llm_getter=lambda: llm)
    assert n == 1
    assert [f["fact"] for f in store.list("user-1", "char-1")] == [ok60]


def test_extract_sync_caps_at_three_facts(mem_env):
    """单轮最多抽 3 条（宁少勿错）。"""
    store, _, card_path = mem_env
    _seed_cards(card_path, {"CARD1": {"character_id": "char-1", "owner_uid": "user-1"}})
    llm = _LLM(json.dumps([f"事实{i}" for i in range(5)], ensure_ascii=False))
    n = cm.extract_sync("user-1", "char-1", "你好", "你好呀", llm_getter=lambda: llm)
    assert n == 3
    assert len(store.list("user-1", "char-1")) == 3


# ───────────────────────── ④ extract_async 后台抽取 ─────────────────────────

class _FakeExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, job):
        self.jobs.append(job)
        return job


def test_extract_async_short_tier_submits_nothing(mem_env, monkeypatch):
    executor = _FakeExecutor()
    monkeypatch.setattr(cm, "_EXECUTOR", executor)
    cm.extract_async("user-1", "char-1", "你好", "你好呀", llm_getter=lambda: _LLM("[]"))
    assert executor.jobs == []


def test_extract_async_long_tier_submits_job(mem_env, monkeypatch):
    _, _, card_path = mem_env
    _seed_cards(card_path, {"CARD1": {"character_id": "char-1", "owner_uid": "user-1"}})
    executor = _FakeExecutor()
    monkeypatch.setattr(cm, "_EXECUTOR", executor)
    called = []
    monkeypatch.setattr(cm, "extract_sync",
                        lambda *a, **k: called.append((a, k)) or 0)

    cm.extract_async("user-1", "char-1", "你好", "你好呀", llm_getter=lambda: None)
    assert len(executor.jobs) == 1
    executor.jobs[0]()  # 手动执行后台任务
    assert len(called) == 1


def test_extract_async_job_exception_swallowed(mem_env, monkeypatch):
    _, _, card_path = mem_env
    _seed_cards(card_path, {"CARD1": {"character_id": "char-1", "owner_uid": "user-1"}})
    executor = _FakeExecutor()
    monkeypatch.setattr(cm, "_EXECUTOR", executor)

    def _boom(*a, **k):
        raise RuntimeError("extract down")

    monkeypatch.setattr(cm, "extract_sync", _boom)
    cm.extract_async("user-1", "char-1", "你好", "你好呀", llm_getter=lambda: None)
    executor.jobs[0]()  # 不抛即通过（后台异常必须全吞）


# ───────────────────────── ⑤ clear 清空 ─────────────────────────

def test_clear_returns_count_and_empties(mem_env):
    store, _, card_path = mem_env
    _seed_cards(card_path, {"CARD1": {"character_id": "char-1", "owner_uid": "user-1"}})
    store.add("user-1", "char-1", "事实一")
    store.add("user-1", "char-1", "事实二")
    assert cm.clear("user-1", "char-1") == 2
    assert store.list("user-1", "char-1") == []


def test_clear_swallows_store_error(mem_env, monkeypatch):
    store, _, _ = mem_env

    def _boom(uid, cid):
        raise RuntimeError("store down")

    monkeypatch.setattr(store, "delete_all", _boom)
    assert cm.clear("user-1", "char-1") == 0
