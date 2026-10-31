"""Embedding Provider 注册与工厂。"""

from fuguoai_core.providers.embedding.base import EmbeddingProvider
from fuguoai_core.providers.embedding.local_hash import LocalHashEmbedding
from fuguoai_core.providers.embedding.local_st_embedding import LocalSTEmbedding


def create_embedding_instance(type_name, config=None):
    """按类型名创建 embedding provider；未知类型回退 local。

    openai / local-st 类型均延迟 import（仅对应 type 时加载重依赖包），
    保证 local 模式零外部依赖，可在无 openai / sentence-transformers 的环境
    离线测试与运行。
    """
    t = (type_name or "local").lower()
    if t == "openai":
        try:
            from fuguoai_core.providers.embedding.openai_embedding import (
                OpenAIEmbedding,
            )

            return OpenAIEmbedding(config or {})
        except Exception as e:
            logger_fallback(e)
            return LocalHashEmbedding(config or {})
    if t in ("local-st", "sentence_transformer", "st"):
        try:
            return LocalSTEmbedding(config or {})
        except Exception as e:
            logger_fallback(e)
            return LocalHashEmbedding(config or {})
    try:
        return LocalHashEmbedding(config or {})
    except Exception as e:
        logger_fallback(e)
        return LocalHashEmbedding(config or {})


def logger_fallback(e):
    try:
        from fuguoai_config.logger import setup_logging

        setup_logging().bind(tag=__name__).warning(
            f"embedding 实例化失败，降级 local: {e}"
        )
    except Exception:
        pass


__all__ = [
    "EmbeddingProvider",
    "LocalHashEmbedding",
    "LocalSTEmbedding",
    "create_embedding_instance",
]
