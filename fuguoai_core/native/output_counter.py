"""按设备维度、按自然日统计 TTS 已合成字数的内存计数器。

计数只驻留内存，进程重启即清零（有意为之）。
"""

import datetime
from typing import Dict

# 设备 -> 当日已合成字数
_device_chars: Dict[str, int] = {}

# 最近一次观察到的“今天”日期；None 表示尚未有过任何写入
_last_seen_day = None


def reset_device_output() -> None:
    """清空所有设备的当日统计。"""
    global _device_chars, _last_seen_day
    _device_chars = {}
    _last_seen_day = None


def get_device_output(device_id: str) -> int:
    """取指定设备当日已合成字数；无记录返回 0。"""
    return _device_chars.get(device_id, 0)


def add_device_output(device_id: str, char_count: int) -> None:
    """累加指定设备当日合成字数；若跨自然日，先整体归零再累加。"""
    global _device_chars, _last_seen_day
    today = datetime.datetime.now().date()
    if _last_seen_day != today:
        # 跨自然日（或首次写入）：整体归零，重新以新的一天起算
        _device_chars = {}
        _last_seen_day = today
    _device_chars[device_id] = _device_chars.get(device_id, 0) + char_count


def check_device_output_limit(device_id: str, max_output_size: int) -> bool:
    """设备当日合成量是否已达上限。device_id 为空字符串时不做限制，返回 False。"""
    if device_id == "":
        return False
    return get_device_output(device_id) >= max_output_size
