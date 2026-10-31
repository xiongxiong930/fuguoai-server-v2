"""VAD（语音活动检测）provider 抽象基类（复果自有实现）。

各 VAD 实现（如 Silero）按连接维度持有自己的检测状态，
对外统一暴露本基类约定的判定入口。
"""

from abc import ABC, abstractmethod


class VADProviderBase(ABC):
    """VAD provider 的统一接口。"""

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
