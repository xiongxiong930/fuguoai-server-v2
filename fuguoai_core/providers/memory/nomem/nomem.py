"""记忆策略：完全关闭记忆。

比「仅上报」更彻底——连上报都不做，服务端对记忆相关链路零介入。
适用于纯对话、不需要任何历史上下文复用的场景。
"""

from ..base import MemoryProviderBase, logger

TAG = __name__


class MemoryProvider(MemoryProviderBase):
    """完全不启用记忆的提供方。"""

    def __init__(self, config, summary_memory=None):
        super().__init__(config)

    async def save_memory(self, msgs, session_id=None, role_id=None):
        logger.bind(tag=TAG).debug("记忆策略=关闭：跳过记忆保存")
        return None

    async def query_memory(self, query: str, role_id=None) -> str:
        logger.bind(tag=TAG).debug("记忆策略=关闭：不执行记忆检索")
        return ""
