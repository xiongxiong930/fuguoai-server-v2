"""记忆策略：仅上报，不落库。

最轻量档。设备侧对话照常进行，但服务端不做任何记忆持久化或总结——
只把本轮聊天记录按既有上报通道送出（供后台分析/审计），不写回任何可检索记忆。
适用于临时会话、演示或隐私敏感场景。
"""

from ..base import MemoryProviderBase, logger

TAG = __name__


class MemoryProvider(MemoryProviderBase):
    """仅上报聊天记录、不做记忆总结的提供方。"""

    def __init__(self, config, summary_memory=None):
        super().__init__(config)

    async def save_memory(self, msgs, session_id=None, role_id=None):
        logger.bind(tag=TAG).debug("记忆策略=仅上报：跳过记忆保存与总结")
        return None

    async def query_memory(self, query: str, role_id=None) -> str:
        logger.bind(tag=TAG).debug("记忆策略=仅上报：不执行记忆检索")
        return ""
