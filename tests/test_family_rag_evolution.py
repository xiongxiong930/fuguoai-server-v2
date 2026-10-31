"""维度⑤ 家庭 RAG 真进化离线测试。

覆盖：LocalHashEmbedding 余弦相关召回、录入检索往返、去重、top_k 截断、
_parse_facts 容错、mock LLM 抽取入库、工厂未知 type 降级 local。
被测逻辑全为纯函数/纯内存，无网络依赖（embedding 用 local）。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fuguoai_config.config_loader import get_project_dir
from fuguoai_core.providers.embedding import (
    LocalHashEmbedding,
    create_embedding_instance,
)
from fuguoai_core.utils.family_rag import FamilyRAG, _parse_facts
from fuguoai_core.utils.util import normalize_device_id

_DEV = "ragtest_001"
_DIR = os.path.join(get_project_dir(), "data", "family_rag")


def _cleanup():
    try:
        p = os.path.join(_DIR, f"{normalize_device_id(_DEV)}.json")
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


def _make_cfg(enabled=True, top_k=3, extract_enabled=True):
    return {
        "FamilyRAG": {
            "enabled": enabled,
            "top_k": top_k,
            "embedding": {"type": "local"},
            "extract": {"enabled": extract_enabled, "llm": "OpenAILLM"},
        },
        "selected_module": {"LLM": "OpenAILLM"},
        "LLM": {
            "OpenAILLM": {"type": "openai", "api_key": "x", "model_name": "m"}
        },
    }


def test_local_embedding_cosine_relevance():
    emb = LocalHashEmbedding()
    cat = emb.embed(["我家猫叫咪咪，是橘猫"])[0]
    q_rel = emb.embed(["猫粮放在哪儿"])[0]
    q_irr = emb.embed(["今天股市涨了"])[0]
    import numpy as np

    def cos(a, b):
        a, b = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))

    assert cos(cat, q_rel) > cos(cat, q_irr), "相关 query 应比不相关 query 更相似"


def test_add_retrieve_roundtrip():
    _cleanup()
    try:
        rag = FamilyRAG(_make_cfg())
        assert rag.add_fact(_DEV, "我家猫叫咪咪，是一只橘猫")
        res = rag.retrieve(_DEV, "猫叫什么名字")
        assert any("咪咪" in r for r in res), f"应召回猫名事实, got {res}"
    finally:
        _cleanup()


def test_dedup_same_content():
    _cleanup()
    try:
        rag = FamilyRAG(_make_cfg())
        rag.add_fact(_DEV, "爸爸对花粉过敏")
        rag.add_fact(_DEV, "爸爸对花粉过敏")  # 重复
        data = rag._load(_DEV)
        assert len(data.get("facts", [])) == 1, "同内容应去重为 1 条"
    finally:
        _cleanup()


def test_render_topk_truncation():
    _cleanup()
    try:
        rag = FamilyRAG(_make_cfg(top_k=3))
        for i in range(10):
            rag.add_fact(_DEV, f"家庭事实编号{i:02d}用于测试截断")
        block = rag.render(_DEV, "家庭事实编号")
        assert block.startswith("<family_knowledge>")
        # top_k=3 应只取前 3 条 "- "
        assert block.count("\n- ") == 3, f"应截断到 3 条, got {block.count(chr(10)+'- ')}"
    finally:
        _cleanup()


def test_parse_facts_robust():
    assert _parse_facts("[]") == []
    arr = _parse_facts('随便 [{"content":"猫叫咪咪","category":"pet","tags":["猫"]}] 结尾')
    assert len(arr) == 1 and arr[0]["content"] == "猫叫咪咪"
    assert _parse_facts("完全没有 json") == []
    assert _parse_facts(None) == []


class _MockLLM:
    def response_no_stream(self, system_prompt, user_prompt):
        yield '[{"content":"我家猫叫咪咪","category":"pet","tags":["猫","咪咪"]},{"content":"爸爸对花粉过敏","category":"health","tags":["过敏"]}]'


def test_extract_with_mock_llm():
    _cleanup()
    try:
        rag = FamilyRAG(_make_cfg())
        rag._set_test_llm(_MockLLM())
        n = rag.extract_and_store(_DEV, "我家猫叫咪咪", "对，是橘猫", _make_cfg())
        assert n == 2, f"应抽取 2 条, got {n}"
        res = rag.retrieve(_DEV, "猫叫什么")
        assert any("咪咪" in r for r in res)
    finally:
        _cleanup()


def test_disabled_shortcut():
    rag = FamilyRAG(_make_cfg(enabled=False))
    assert rag.render(_DEV, "任何问题") == ""
    assert rag.retrieve(_DEV, "任何问题") == []


def test_zero_overlap_filter():
    _cleanup()
    try:
        rag = FamilyRAG(_make_cfg())
        rag.add_fact(_DEV, "我家猫叫咪咪，是一只橘猫")
        # 完全不相关 query（零 token 重叠）必须不召回
        assert rag.retrieve(_DEV, "今天股票涨了吗") == []
        # 相关 query 仍召回
        assert any("咪咪" in r for r in rag.retrieve(_DEV, "猫叫什么名字"))
    finally:
        _cleanup()


def test_factory_fallback_unknown_type():
    inst = create_embedding_instance("不存在的类型", {})
    assert isinstance(inst, LocalHashEmbedding)


# ---- 相关度闸门（min_score / rel_gap）----
# 用确定性向量直测语义，不依赖任何 embedding 后端的分数分布。
_VEC = [
    [1.0, 0.0],          # cos=1.00
    [0.9, 0.436],        # cos≈0.90
    [0.6, 0.8],          # cos=0.60
    [0.1, 0.995],        # cos≈0.10
]


def _vec_facts():
    return [{"content": f"f{i}", "vec": v} for i, v in enumerate(_VEC)]


def test_gate_default_off_zero_regression():
    """默认 min_score/rel_gap=0：行为与加闸门之前完全一致（零回归）。"""
    rag = FamilyRAG(_make_cfg())
    assert rag.min_score == 0.0 and rag.rel_gap == 0.0
    got = [f["content"] for f in FamilyRAG._cosine_topk([1.0, 0.0], _vec_facts(), 5)]
    assert got == ["f0", "f1", "f2", "f3"], f"默认应不过滤, got {got}"


def test_gate_min_score_absolute():
    """min_score 绝对下限：低于阈值的条目不注入。"""
    got = [f["content"] for f in FamilyRAG._cosine_topk(
        [1.0, 0.0], _vec_facts(), 5, min_score=0.6)]
    assert got == ["f0", "f1", "f2"], f"min_score=0.6 应留 3 条, got {got}"
    got = [f["content"] for f in FamilyRAG._cosine_topk(
        [1.0, 0.0], _vec_facts(), 5, min_score=0.95)]
    assert got == ["f0"], f"min_score=0.95 应只留 top1, got {got}"
    # 全部低于阈值 ⇒ 不注入（无关问句场景）
    assert FamilyRAG._cosine_topk([1.0, 0.0], _vec_facts(), 5, min_score=1.01) == []


def test_gate_rel_gap_drops_companions():
    """rel_gap 相对衰减：挡掉远逊于 top1 的陪跑项。"""
    got = [f["content"] for f in FamilyRAG._cosine_topk(
        [1.0, 0.0], _vec_facts(), 5, rel_gap=0.2)]
    assert got == ["f0", "f1"], f"rel_gap=0.2 应只留 ≥0.8 的 2 条, got {got}"
    # 两者取大：min_score 更高时以 min_score 为准
    got = [f["content"] for f in FamilyRAG._cosine_topk(
        [1.0, 0.0], _vec_facts(), 5, min_score=0.95, rel_gap=0.2)]
    assert got == ["f0"], f"应取 max(min_score, top1-rel_gap), got {got}"


def test_gate_wired_into_retrieve():
    """闸门经配置贯通到 retrieve：配高阈值后不相关事实不再召回。"""
    _cleanup()
    try:
        rag = FamilyRAG(_make_cfg())
        rag.min_score = 1.01  # 等价于"什么都挡掉"
        rag.add_fact(_DEV, "我家猫叫咪咪，是一只橘猫")
        assert rag.retrieve(_DEV, "猫叫什么名字") == [], "超阈值应不召回"
        rag.min_score = 0.0
        assert rag.retrieve(_DEV, "猫叫什么名字"), "放开阈值应恢复召回"
    finally:
        _cleanup()


if __name__ == "__main__":
    test_local_embedding_cosine_relevance()
    test_add_retrieve_roundtrip()
    test_dedup_same_content()
    test_render_topk_truncation()
    test_parse_facts_robust()
    test_extract_with_mock_llm()
    test_disabled_shortcut()
    test_zero_overlap_filter()
    test_factory_fallback_unknown_type()
    test_gate_default_off_zero_regression()
    test_gate_min_score_absolute()
    test_gate_rel_gap_drops_companions()
    test_gate_wired_into_retrieve()
    print("ALL FAMILY_RAG TESTS PASSED")
