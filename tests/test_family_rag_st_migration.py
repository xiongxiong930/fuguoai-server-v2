"""L3：FamilyRAG 向量迁移 _ensure_vectors（不依赖 torch / sentence-transformers）。

验证维度⑤切换 embedding 后端时的兼容性：
  1) 切换到新 provider 后，已存事实的 vec 按当前 provider 重算，emb_type 同步更新；
  2) 遗留事实（无 emb_type，视为 local）在切换后会被正确迁移；
  3) provider 暂不可用（返回零向量）时不破坏数据、不无限循环，可重试。
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fuguoai_core.providers.embedding.base import EmbeddingProvider
from fuguoai_core.utils.family_rag import FamilyRAG


class _StubEmb(EmbeddingProvider):
    """用 tag 区分的确定性假 provider，模拟「不同向量空间」。"""

    type_key = "stub"

    def __init__(self, config=None, dim=4, tag=None):
        super().__init__(config)
        self.dim = dim
        self.tag = tag or "A"

    def _embed_batch(self, texts):
        return [
            [float(hash(self.tag + t) % 100) / 100.0] + [0.0] * (self.dim - 1)
            for t in texts
        ]


def _make_rag(tmp):
    cfg = {"FamilyRAG": {"enabled": True, "embedding": {"type": "local"}}}
    rag = FamilyRAG(cfg)
    rag._dir = os.path.join(tmp, "data", "family_rag")
    return rag


def test_migration_on_switch():
    tmp = tempfile.mkdtemp()
    try:
        rag = _make_rag(tmp)
        rag.embedding = _StubEmb(tag="A")
        rag._emb_type = "A"
        assert rag.add_fact("dev1", "爸爸每周三晚上回来吃饭", category="schedule")
        vec_a = rag._load("dev1")["facts"][0]["vec"]
        assert rag._load("dev1")["facts"][0]["emb_type"] == "A"

        # 切换到 provider B（不同向量空间）
        rag.embedding = _StubEmb(tag="B")
        rag._emb_type = "B"
        got = rag.retrieve("dev1", "爸爸什么时候回家")
        assert "爸爸每周三晚上回来吃饭" in got, got
        f = rag._load("dev1")["facts"][0]
        assert f["emb_type"] == "B"
        assert f["vec"] != vec_a  # 确实重算了
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_legacy_facts_migrate():
    tmp = tempfile.mkdtemp()
    try:
        rag = _make_rag(tmp)
        rag.embedding = _StubEmb(tag="A")
        rag._emb_type = "A"
        os.makedirs(rag._dir, exist_ok=True)
        p = os.path.join(rag._dir, "dev2.json")
        import json

        with open(p, "w", encoding="utf-8") as fp:
            json.dump(
                {
                    "facts": [
                        {
                            "id": "x",
                            "content": "猫粮在厨房左边柜子里",
                            "category": "item",
                            "tags": [],
                            "vec": [1.0, 2.0, 3.0, 4.0],
                            "ts": 1,
                        }
                    ]
                },
                fp,
            )
        rag.embedding = _StubEmb(tag="B")
        rag._emb_type = "B"
        got = rag.retrieve("dev2", "猫粮放在哪")
        assert "猫粮在厨房左边柜子里" in got, got
        assert rag._load("dev2")["facts"][0]["emb_type"] == "B"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_broken_provider_no_loop():
    tmp = tempfile.mkdtemp()
    try:
        rag = _make_rag(tmp)
        rag.embedding = _StubEmb(tag="A")
        rag._emb_type = "A"
        assert rag.add_fact("dev3", "咪咪是一只橘猫", category="pet")

        class _Zero(_StubEmb):
            def _embed_batch(self, texts):
                return [[0.0] * self.dim for _ in texts]

        rag.embedding = _Zero(tag="B", dim=4)
        rag._emb_type = "B"
        got = rag.retrieve("dev3", "猫叫什么")
        # 零向量不迁移、不崩；旧 A 向量对 B 查询余弦≈0 → 不过滤阈值 → 空
        assert rag._load("dev3")["facts"][0]["emb_type"] == "A"
        assert got == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_migration_on_switch()
    test_legacy_facts_migrate()
    test_broken_provider_no_loop()
    print("ALL PASS")
