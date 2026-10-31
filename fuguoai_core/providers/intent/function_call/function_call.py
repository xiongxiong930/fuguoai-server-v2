# -*- coding: utf-8 -*-
"""复果AI · function_call 模式意图 Provider

该模式下意图识别完全交给主对话 LLM 的原生工具调用能力，本 Provider
只需恒定返回 continue_chat，让对话主流程带工具直出。
"""

from typing import Dict, List

from fuguoai_config.logger import setup_logging

from ..base import IntentProviderBase

TAG = __name__
logger = setup_logging()

_PASS_THROUGH = '{"function_call": {"name": "continue_chat"}}'


class IntentProvider(IntentProviderBase):
    async def detect_intent(self, conn, dialogue_history: List[Dict], text: str) -> str:
        """恒定放行：意图判定由主 LLM 的 function calling 完成。"""
        logger.bind(tag=TAG).debug(
            "Using functionCallProvider, always returning continue chat"
        )
        return _PASS_THROUGH
