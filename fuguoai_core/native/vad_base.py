"""VAD（语音活动检测）provider 的抽象基类。

各 VAD 实现（如 Silero）按连接维度持有自己的检测状态，对外统一暴露
本基类约定的判定入口。本模块仅依赖 Python 标准库，不引入任何具体
VAD 算法，也不规定 `conn` / `data` 的具体类型（由子类解释）。
"""

from abc import ABC, abstractmethod


class VADProviderBase(ABC):
    """VAD provider 的抽象基类。

    子类必须实现 `is_vad` 以给出具体的语音活动判定逻辑；基类本身不可
    直接实例化。
    """

    @abstractmethod
    def is_vad(self, conn, data) -> bool:
        """判定一段音频数据是否包含语音活动。

        Args:
            conn: 当前设备连接上下文，实现可据其保存/读取检测状态。
            data: 待判定的音频数据块。

        Returns:
            判定为"有语音活动"时返回 True，否则返回 False。
        """
        raise NotImplementedError
