"""LocalSTEmbedding —— 本地 fastembed 语义向量（离线 / 隐私 / 轻量）。

维度⑤升级为「真语义」的本地方案：家庭事实与查询向量均在容器内计算，
不上送任何外部服务（相对 OpenAIEmbedding 的隐私优势，且不引入任何 API key）。

实现要点：
  - 使用 fastembed（ONNX Runtime 后端，不依赖 torch），内存 / 镜像占用远低于
    sentence-transformers，适合本服务所在的小内存容器（3.7G 主机实测 torch 版 OOM）。
  - 默认模型 BAAI/bge-small-zh-v1.5（中文强，512 维，ONNX 体积极小；与
    LocalHashEmbedding 同维以避免维度守卫误跳，但向量空间不同仍需迁移）。
  - 模型首次 embed 时惰性加载并缓存到进程（线程安全）。
  - 模型缓存目录固定在 /opt/fuguoai-server/models/embedding_cache（容器内持久烘焙路径，
    **非 data/tmp**）：避免 TMPDIR 指向 bind 挂载目录导致清 tmp 时模型被删、重启重下 91M。
  - embed() 基类已处理文本级缓存与异常兜底（失败返回零向量），本类只实现 _embed_batch。
"""

import threading

from fuguoai_core.providers.embedding.base import EmbeddingProvider

_DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"


class LocalSTEmbedding(EmbeddingProvider):
    type_key = "local-st"

    def __init__(self, config=None):
        super().__init__(config)
        cfg = config or {}
        self.model_name = cfg.get("model_name") or _DEFAULT_MODEL
        # 持久烘焙路径(非 data/tmp)：清 tmp / 重启都不会丢失或重下模型
        self.cache_dir = cfg.get("cache_dir") or "/opt/fuguoai-server/models/embedding_cache"
        self._model = None
        self._load_lock = threading.Lock()
        self.dim = 0

    def _load_model(self):
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            from fastembed import TextEmbedding

            model = TextEmbedding(model_name=self.model_name, cache_dir=self.cache_dir)
            self._model = model
        return self._model

    def _embed_batch(self, texts):
        model = self._load_model()
        # fastembed.embed 返回 numpy 数组的 generator（默认已 L2 归一化）
        vecs = list(model.embed(list(texts), batch_size=16, show_progress_bar=False))
        if self.dim == 0 and vecs:
            self.dim = int(len(vecs[0]))
        return [v.astype("float32").tolist() for v in vecs]
