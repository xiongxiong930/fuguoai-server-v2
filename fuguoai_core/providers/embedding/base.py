"""Embedding Provider 抽象基类。

家庭 RAG（维度⑤）的可插拔向量化层。所有实现提供：
  - embed(texts: list[str]) -> list[list[float]]（带进程级缓存）
  - dim 属性（向量维度）

内置实现：
  - LocalHashEmbedding：纯 numpy TF-IDF（hashing trick），零外部依赖，
    用于离线测试与远程 embeddings 不可用时的降级（保证部署不阻塞）。
  - OpenAIEmbedding：openai-compatible 远程语义向量（真语义来源）。
"""

import hashlib
import threading
from abc import ABC, abstractmethod


class EmbeddingProvider(ABC):
    def __init__(self, config=None):
        self._cache = {}
        self._cache_lock = threading.Lock()
        self.dim = 0

    @abstractmethod
    def _embed_batch(self, texts):
        """子类实现：批量向量化，返回与 texts 等长、维度 self.dim 的向量列表。"""
        raise NotImplementedError

    def embed(self, texts):
        """向量化（带文本级缓存）。texts 可为字符串或列表。绝不抛异常由子类保证。"""
        if isinstance(texts, str):
            texts = [texts]
        out = []
        to_embed = []
        idx_map = {}
        with self._cache_lock:
            for i, t in enumerate(texts):
                t = t or ""
                h = hashlib.md5(t.encode("utf-8")).hexdigest()
                if h in self._cache:
                    out.append(self._cache[h])
                else:
                    idx_map[len(to_embed)] = (i, h)
                    to_embed.append(t)
        if to_embed:
            try:
                vecs = self._embed_batch(to_embed)
            except Exception:
                vecs = [self._zero_vec() for _ in to_embed]
            with self._cache_lock:
                for j, (i, h) in idx_map.items():
                    vec = vecs[j] if j < len(vecs) else self._zero_vec()
                    self._cache[h] = vec
                    out.append(vec)
        return out

    def _zero_vec(self):
        return [0.0] * (self.dim or 1)
