# -*- coding: utf-8 -*-
"""意图识别 Provider 抽象基类（复果原生实现，clean-room 重写）。

约定：具体实现（intent_llm / function_call / nointent）统一命名为
``IntentProvider``，由 fuguoai_core/utils/intent.py 按配置动态装载。
"""

from abc import ABC, abstractmethod
from typing import Dict, List

from fuguoai_config.logger import setup_logging

_log = setup_logging()


class IntentProviderBase(ABC):
    def __init__(self, config):
        self.config = config

    def set_llm(self, llm):
        """注入意图识别所用的 LLM 实例（可与主对话 LLM 不同）。"""
        self.llm = llm
        _nm = getattr(llm, "model_name", llm.__class__.__name__)
        _log.info(f"intent provider llm -> {_nm}")

    @abstractmethod
    async def detect_intent(self, conn, dialogue_history: List[Dict], text: str) -> str:
        """识别用户最后一句话的意图。

        入参 dialogue_history 为对话历史（含 role/content），text 为本轮用户输入；
        返回 JSON 字符串（function_call 结构）或意图短语，如"继续聊天"、
        "结束聊天"、"播放音乐 歌名"、"查询天气 地点名"等。
        """
        pass
