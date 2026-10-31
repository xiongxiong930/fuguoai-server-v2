"""TTS 相关数据传输对象（复果自有实现）。"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SentenceType(Enum):
    """一句话在整段播报中所处的位置。"""

    FIRST = "FIRST"    # 首句
    MIDDLE = "MIDDLE"  # 中间句
    LAST = "LAST"      # 末句


class ContentType(Enum):
    """待合成内容的形态。"""

    TEXT = "TEXT"      # 纯文本
    FILE = "FILE"      # 音频文件
    ACTION = "ACTION"  # 动作/控制指令


class InterfaceType(Enum):
    """TTS 的接入形态。"""

    DUAL_STREAM = "DUAL_STREAM"      # 双流式
    SINGLE_STREAM = "SINGLE_STREAM"  # 单流式
    NON_STREAM = "NON_STREAM"        # 非流式


@dataclass
class TTSMessageDTO:
    """一次 TTS 合成请求的描述。"""

    sentence_id: str
    sentence_type: SentenceType
    content_type: ContentType
    content_detail: Optional[str] = None  # 待合成文本，或音频对应的歌词
    content_file: Optional[str] = None    # content_type 为 FILE 时的文件路径
