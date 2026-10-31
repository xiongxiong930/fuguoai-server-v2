"""ASR 相关数据传输对象（复果自有实现）。"""

from enum import Enum


class InterfaceType(Enum):
    """ASR 的接入形态。"""

    STREAM = "STREAM"          # 流式识别：边收音频边出结果
    NON_STREAM = "NON_STREAM"  # 整段识别：说完一整句再出结果
    LOCAL = "LOCAL"            # 端侧/本地识别：不出设备、不上云
