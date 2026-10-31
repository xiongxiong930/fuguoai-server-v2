"""设备当日 TTS 合成量统计（clean-room 重写）。

用于按设备维度限制每日合成量：跨自然日自动归零，单设备按天累加。
计数只驻留内存，进程重启即清零——这是有意为之，不追求跨重启持久化。

实现独立编写：把「当日账本」封装成 `_DayLedger`（自记归属日期，日期一变即
整体作废），模块级只保留一个实例，公开函数为薄封装；跨日判定收在 `_rollover`
一处，不在多个函数里各自比较日期。对外仍只暴露
`reset_device_output` / `get_device_output` / `add_device_output` /
`check_device_output_limit` 四个入口。
"""

import datetime
from typing import Dict, Optional


class _DayLedger:
    """单日合成量账本：记住自己归属哪一天，日期一变即整体作废。"""

    def __init__(self):
        self._day: Optional[datetime.date] = None
        self._counts: Dict[str, int] = {}

    def _rollover(self) -> datetime.date:
        """跨自然日时清空计数；返回今日日期供本次操作使用。"""
        today = datetime.datetime.now().date()
        if self._day != today:
            self._counts.clear()
            self._day = today
        return today

    def bump(self, device_id: str, char_count: int) -> None:
        """给指定设备累加字数（写入前先做跨日检查）。"""
        self._rollover()
        self._counts[device_id] = self._counts.get(device_id, 0) + char_count

    def read(self, device_id: str) -> int:
        """取指定设备当日字数；读取同样走跨日检查，保证不返回昨天的数据。"""
        self._rollover()
        return self._counts.get(device_id, 0)

    def purge(self) -> None:
        """清空全部设备的当日计数（每日 0 点调用）。"""
        self._counts.clear()


_ledger = _DayLedger()


def reset_device_output() -> None:
    """清空所有设备的当日统计（每日 0 点调用）。"""
    _ledger.purge()


def get_device_output(device_id: str) -> int:
    """取指定设备当日的已合成字数，无记录返回 0。"""
    return _ledger.read(device_id)


def add_device_output(device_id: str, char_count: int) -> None:
    """累加指定设备当日合成字数；跨自然日时先整体归零再累加。"""
    _ledger.bump(device_id, char_count)


def check_device_output_limit(device_id: str, max_output_size: int) -> bool:
    """设备当日合成量是否已达上限。

    device_id 为空时不做限制（视为无需统计的匿名调用）。
    """
    if not device_id:
        return False
    return get_device_output(device_id) >= max_output_size
