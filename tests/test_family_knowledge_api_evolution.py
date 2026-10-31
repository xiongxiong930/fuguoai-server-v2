"""家庭知识录入 API 离线测试（维度⑤延伸，真进化）。

钉死 FamilyRAG 管理方法的纯逻辑（list/upsert/remove/clear）：
- 列表不含向量、按 ts 倒序
- 主动录入往返 + 相同 content 视为同一条（sha1 同 id，不去重新增）
- category/tags 规整；未知 category 回退 other
- 删除按 id；清空返回删除数
- disabled 短路（enabled=False 时 list 返回空、upsert 返回 None）
- content 过短（<2）录入返回 None
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fuguoai_core.utils.family_rag import FamilyRAG, _CATEGORIES


def _make_cfg(enabled=True, tmp=None):
    cfg = {"FamilyRAG": {"enabled": enabled, "top_k": 5,
                          "embedding": {"type": "local"},
                          "extract": {"enabled": True}},
           "selected_module": {"LLM": "OpenAILLM"}}
    if tmp is not None:
        # 重定向存储目录，避免污染真实 data/
        import fuguoai_core.utils.family_rag as fr
        fr.get_project_dir = lambda: tmp
    return cfg


def test_list_empty():
    with tempfile.TemporaryDirectory() as tmp:
        rag = FamilyRAG(_make_cfg(tmp=tmp))
        assert rag.list_facts("d1") == [], "空设备应返回 []"
        # 列表不应含 vec 字段
        f = rag.upsert_fact("d1", "我家猫叫咪咪")
        assert f is not None and "vec" not in f, "对外条目不含向量"
        listed = rag.list_facts("d1")
        assert len(listed) == 1 and "vec" not in listed[0]


def test_upsert_roundtrip_and_dedup():
    with tempfile.TemporaryDirectory() as tmp:
        rag = FamilyRAG(_make_cfg(tmp=tmp))
        f1 = rag.upsert_fact("d1", "我家猫叫咪咪", "pet", ["猫", "咪咪"])
        assert f1["content"] == "我家猫叫咪咪"
        assert f1["category"] == "pet"
        assert f1["tags"] == ["猫", "咪咪"]
        # 相同 content 视为同一条（id 同），不去重新增
        f2 = rag.upsert_fact("d1", "我家猫叫咪咪", "other", None)
        assert f2["id"] == f1["id"], "相同 content 应同 id"
        assert f2["category"] == "other", "更新应覆盖 category"
        assert len(rag.list_facts("d1")) == 1, "同 content 不去重新增"
        # 不同 content 独立
        rag.upsert_fact("d1", "爸爸对花粉过敏", "health", ["过敏"])
        assert len(rag.list_facts("d1")) == 2


def test_category_and_tags_normalization():
    with tempfile.TemporaryDirectory() as tmp:
        rag = FamilyRAG(_make_cfg(tmp=tmp))
        # 未知 category 回退 other
        f = rag.upsert_fact("d1", "客厅有绿植", "not_a_cat", None)
        assert f["category"] == "other"
        # tags 逗号串自动拆
        f2 = rag.upsert_fact("d1", "孩子叫小明", "family", "小明,儿子")
        assert f2["tags"] == ["小明", "儿子"]
        assert "not_a_cat" not in _CATEGORIES


def test_remove_and_clear():
    with tempfile.TemporaryDirectory() as tmp:
        rag = FamilyRAG(_make_cfg(tmp=tmp))
        f = rag.upsert_fact("d1", "药箱在客厅抽屉", "item")
        assert rag.remove_fact("d1", f["id"]) is True
        assert rag.list_facts("d1") == []
        assert rag.remove_fact("d1", f["id"]) is False, "删不存在返回 False"
        rag.upsert_fact("d1", "条目甲")
        rag.upsert_fact("d1", "条目乙")
        n = rag.clear("d1")
        assert n == 2
        assert rag.list_facts("d1") == []


def test_disabled_shortcut():
    with tempfile.TemporaryDirectory() as tmp:
        rag = FamilyRAG(_make_cfg(enabled=False, tmp=tmp))
        assert rag.list_facts("d1") == []
        assert rag.upsert_fact("d1", "任意内容") is None, "disabled 时录入返回 None"
        assert rag.clear("d1") == 0


def test_content_too_short():
    with tempfile.TemporaryDirectory() as tmp:
        rag = FamilyRAG(_make_cfg(tmp=tmp))
        assert rag.upsert_fact("d1", "") is None
        assert rag.upsert_fact("d1", "x") is None, "单字应被拒"


def test_get_family_rag_no_arg_returns_instance():
    """回归：routes 以无参调用 get_family_rag() 不得抛 TypeError（曾因参数必填导致 500）。"""
    from fuguoai_core.utils.family_rag import get_family_rag
    inst = get_family_rag()          # 无参：修复前 TypeError -> 500
    assert inst is not None, "无参 get_family_rag 应返回已初始化单例"
    again = get_family_rag()         # 再次无参：应为同一单例且不被误改 enabled
    assert again is inst, "应为进程级单例"


def run_all():
    test_list_empty()
    test_upsert_roundtrip_and_dedup()
    test_category_and_tags_normalization()
    test_remove_and_clear()
    test_disabled_shortcut()
    test_content_too_short()
    test_get_family_rag_no_arg_returns_instance()
    print("ALL FAMILY_KNOWLEDGE_API TESTS PASSED")


if __name__ == "__main__":
    run_all()
