"""LocalHashEmbedding —— 纯 numpy TF-IDF 向量（零外部依赖）。

用于：① 离线单元测试（真进化条款要求离线可测）② 远程 embeddings 不可用时的
降级方案（保证家庭 RAG 部署立即可用、绝不阻塞主对话）。

实现要点：
  - 分词：英文按词、中文按字符 + 字符 bigram（无需 jieba，对中文友好）
  - 停用词过滤（中文常见虚词）
  - hashing trick 固定维度（512），TF(log1p) 加权，L2 归一化
  - 余弦相似度 = 点积（因已 L2 归一化）
注：纯词面向量，语义能力有限；配置 OpenAIEmbedding 即升级真语义。
"""

import hashlib
import math
import re
from collections import Counter

import numpy as np

from fuguoai_core.providers.embedding.base import EmbeddingProvider

# 中文常见停用词（精简集，缓解虚词占权重）
_STOP = set(
    "的 了 是 在 我 你 他 她 它 们 这 那 有 和 与 及 也 都 就 还 把 被 让 给 "
    "对 从 到 上 下 里 中 个 之 其 此 该 我们 你们 他们 没有 不 会 能 要 想 "
    "说 知道 一个 这个 那个 什么 怎么 怎样 如何".split()
)

_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+|[一-鿿]")


def _tokenize(text):
    toks = []
    for m in _TOKEN_RE.finditer((text or "").lower()):
        s = m.group(0)
        if s.isascii():
            if s not in _STOP:
                toks.append(s)
        else:
            for ch in s:
                if ch not in _STOP:
                    toks.append(ch)
            for i in range(len(s) - 1):
                bg = s[i : i + 2]
                if not all(c in _STOP for c in bg):
                    toks.append("##" + bg)
    return toks


class LocalHashEmbedding(EmbeddingProvider):
    DIM = 512

    def __init__(self, config=None):
        super().__init__(config)
        self.dim = self.DIM

    def _embed_batch(self, texts):
        vecs = []
        for text in texts:
            toks = _tokenize(text)
            vec = np.zeros(self.DIM, dtype=np.float32)
            if not toks:
                vecs.append(vec.tolist())
                continue
            c = Counter(toks)
            norm = 0.0
            for tok, f in c.items():
                h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16) % self.DIM
                w = math.log1p(f)
                vec[h] += w
                norm += w * w
            if norm > 0:
                vec /= math.sqrt(norm)
            vecs.append(vec.tolist())
        return vecs
