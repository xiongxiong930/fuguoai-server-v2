"""记忆能力抽象层（复果自有实现）。

定义所有记忆提供方的统一契约：保存与查询。具体策略（是否落库、是否做总结、
是否接声纹/向量检索）由各子类实现，本基类只固定接口形状与共享的初始化流程，
不绑定任何上游实现细节。
"""

from abc import ABC, abstractmethod

from fuguoai_config.logger import setup_logging

TAG = __name__
logger = setup_logging()


class MemoryProviderBase(ABC):
    """记忆提供方基类：子类须实现保存与查询两个异步接口。"""

    def __init__(self, config):
        self.config = config
        # 当前生效的角色标识；未绑定角色时为 None
        self.role_id = None

    def set_llm(self, llm):
        """挂载对话 LLM 实例，供需要复述/总结的子类使用。"""
        self.llm = llm

    @abstractmethod
    async def save_memory(self, msgs, session_id=None, role_id=None):
        """保存一轮对话记忆，返回记忆记录 ID（无持久化时返回 None）。"""
        # 抽象方法占位：具体策略由子类决定，基类不实现任何落库逻辑
        logger.bind(tag=TAG).debug("基类 save_memory 为抽象接口，未执行任何保存")
        return None

    @abstractmethod
    async def query_memory(self, query: str, role_id=None) -> str:
        """按相似度检索指定角色的记忆，返回可注入上下文的文本片段。"""
        # 抽象方法占位：未实现时返回空串，避免下游误判为有记忆
        logger.bind(tag=TAG).debug("基类 query_memory 为抽象接口，返回空结果")
        return ""

    def init_memory(self, role_id, llm, **kwargs):
        """初始化记忆上下文：绑定角色与 LLM，供整轮对话复用。"""
        self.role_id = role_id
        self.llm = llm
