"""OpenAIEmbedding —— openai-compatible 远程语义向量（真语义来源）。

复用 LLM 的 openai 客户端范式（base_url/url + api_key + model_name），
调 client.embeddings.create(...)。需要 openai Python 包（容器已具备）。

配置（Embedding 块，与 LLM 同范式）：
  type: openai
  base_url: https://api.openai.com/v1   # 或兼容网关
  model_name: text-embedding-3-small
  api_key: sk-xxx
  dim: 1536                            # 可选，不匹配时自动探测
"""

import threading
import time

import openai

from fuguoai_config.logger import setup_logging
from fuguoai_core.providers.embedding.base import EmbeddingProvider

TAG = __name__
logger = setup_logging()


class OpenAIEmbedding(EmbeddingProvider):
    def __init__(self, config=None):
        super().__init__(config)
        cfg = config or {}
        api_key = cfg.get("api_key") or ""
        base_url = cfg.get("base_url") or cfg.get("url")
        self.model = cfg.get("model_name") or "text-embedding-3-small"
        self.dim = int(cfg.get("dim", 0) or 0)
        custom_timeout = float(cfg.get("timeout", 15.0))
        self.client = openai.OpenAI(
            api_key=api_key, base_url=base_url, timeout=custom_timeout
        )
        # 维度探测（首次调用时惰性确定，避免构造期网络）
        self._dim_probed = False
        self._probe_lock = threading.Lock()

    def _embed_batch(self, texts):
        resp = self.client.embeddings.create(model=self.model, input=texts)
        vecs = [list(d.embedding) for d in resp.data]
        if not self._dim_probed:
            with self._probe_lock:
                if not self._dim_probed and vecs:
                    self.dim = len(vecs[0])
                    self._dim_probed = True
        return vecs
