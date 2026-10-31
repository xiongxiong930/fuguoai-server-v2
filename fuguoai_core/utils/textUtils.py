"""
复果文本工具集。

集中处理对话文本里的「标点 / 表情 / 情绪」三类轻量文本操作：
首尾裁剪、emoji 剔除、情绪标签识别与下发。全部为纯文本处理，
不触网、不依赖任何外部服务，可被 TTS / LLM 回包后处理安全复用。

对外符号名与两张映射表（EMOJI_MAP / EMOJI_RANGES）与设备端协议绑定，
改动需同步固件，详见 EVIDENCE_LEDGER.md。
"""

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler

TAG = __name__

# 表情字符 -> 情绪标签。标签值由设备端固件驱动表情动画，属线上协议不可改名。
EMOJI_MAP = {
    "😂": "funny",
    "😭": "crying",
    "😠": "angry",
    "😔": "sad",
    "😍": "loving",
    "😲": "surprised",
    "😱": "shocked",
    "🤔": "thinking",
    "😌": "relaxed",
    "😴": "sleepy",
    "😜": "silly",
    "🙄": "confused",
    "😶": "neutral",
    "🙂": "happy",
    "😆": "laughing",
    "😳": "embarrassed",
    "😉": "winking",
    "😎": "cool",
    "🤤": "delicious",
    "😘": "kissy",
    "😏": "confident",
}

# emoji 主体所在的 Unicode 区块闭区间（标准区块划分，非自定义）。
EMOJI_RANGES = [
    (0x1F600, 0x1F64F),
    (0x1F300, 0x1F5FF),
    (0x1F680, 0x1F6FF),
    (0x1F900, 0x1F9FF),
    (0x1FA70, 0x1FAFF),
    (0x2600, 0x26FF),
    (0x2700, 0x27BF),
]

# 首尾裁剪要剔除的中英文标点（全角与半角并列）。用字符串展开以省去逐个引号。
_PUNCTUATION_SET = set("，,。.！!“”\":：－-、[]【】")

# get_emotion 未命中时的兜底表情与标签
_FALLBACK_EMOJI = "🙂"
_FALLBACK_EMOTION = "happy"


def is_emoji(char: str) -> bool:
    """判断单个字符是否落在 emoji 区块内。"""
    code_point = ord(char)
    for start, end in EMOJI_RANGES:
        if start <= code_point <= end:
            return True
    return False


def is_punctuation_or_emoji(char: str) -> bool:
    """判断字符是否为「空白 / 标点 / emoji」——即首尾裁剪要剥掉的那类字符。"""
    return char.isspace() or char in _PUNCTUATION_SET or is_emoji(char)


def get_string_no_punctuation_or_emoji(text: str) -> str:
    """剥掉文本首尾连续的空白、标点与表情，中间内容原样保留。"""
    lo = 0
    hi = len(text)
    while lo < hi and is_punctuation_or_emoji(text[lo]):
        lo += 1
    while hi > lo and is_punctuation_or_emoji(text[hi - 1]):
        hi -= 1
    return text[lo:hi]


def _keep_for_plain_text(char: str) -> bool:
    """纯文本筛选：非换行、非 emoji 的字符保留。"""
    return char != "\n" and not is_emoji(char)


def check_emoji(text: str) -> str:
    """剔除文本中全部 emoji 与换行符，返回可直接送 TTS 的纯文本。"""
    return "".join(filter(_keep_for_plain_text, text))


async def get_emotion(conn: "ConnectionHandler", text: str) -> None:
    """取文本中首个情绪表情并下发设备；整段无表情时用兜底的微笑。"""
    emoji, emotion = _FALLBACK_EMOJI, _FALLBACK_EMOTION
    for char in text:
        if char in EMOJI_MAP:
            emoji = char
            emotion = EMOJI_MAP[char]
            break
    try:
        await conn.safe_send(
            json.dumps(
                {
                    "type": "llm",
                    "text": emoji,
                    "emotion": emotion,
                    "session_id": conn.session_id,
                }
            )
        )
    except Exception as e:
        conn.logger.bind(tag=TAG).warning(f"下发情绪表情失败：{e}")
