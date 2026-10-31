# -*- coding: utf-8 -*-
"""复果AI · 关闭意图识别的空实现 Provider

配置选择 nointent 时装载本类：不做任何判定，所有输入一律按普通聊天处理。
"""

from typing import Dict, List

from fuguoai_config.logger import setup_logging

from ..base import IntentProviderBase

TAG = __name__
logger = setup_logging()

_PASS_THROUGH = '{"function_call": {"name": "continue_chat"}}'


class IntentProvider(IntentProviderBase):
    async def detect_intent(self, conn, dialogue_history: List[Dict], text: str) -> str:
        """恒定返回继续聊天（意图识别关闭态）。"""
        logger.bind(tag=TAG).debug(
            "Using NoIntentProvider, always returning continue chat"
        )
        return _PASS_THROUGH
