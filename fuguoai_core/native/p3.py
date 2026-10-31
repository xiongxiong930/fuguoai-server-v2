"""p3 容器解码模块（clean-room 独立重写）。

p3 是承载 opus 包的极简容器格式，由逐条记录拼接而成。本模块只负责解码，
对外提供「流式（逐包回调）」与「一次性（返回列表）」两种形态。

记录格式（硬契约）：
    记录 = 4 字节头 + 负载
    头   = 大端 '>BBH' ：依次 (type:1B, reserved:1B, payload_len:2B)
    负载 = payload_len 字节，一个完整的 opus 包

解码只消费 payload_len；type / reserved 解码时读取但忽略。

依赖：仅 Python 标准库（io、struct、typing）。
"""

import io
import struct
from typing import BinaryIO, Callable, Iterator, List

# 记录头布局：大端 (type:1B, reserved:1B, payload_len:2B)
_HEADER_STRUCT = struct.Struct(">BBH")
_HEADER_SIZE = _HEADER_STRUCT.size  # 4


def _iter_p3_packets(stream: BinaryIO) -> Iterator[bytes]:
    """从二进制流中逐条解出 opus 负载。

    对"半包"零容忍：头部不完整（读到 1~3 字节）或负载不足声明长度，
    一律抛 ValueError，绝不放行不完整数据给下游。读到 0 字节视为正常结束。

    :param stream: 提供 read(n) 的二进制流对象
    :yield: 每条记录解出的 opus 负载字节
    """
    while True:
        header = stream.read(_HEADER_SIZE)
        if len(header) == 0:
            # 流尾，正常结束
            return
        if len(header) < _HEADER_SIZE:
            # 声明了记录开始却不足头长 —— 头部被截断
            raise ValueError(
                "p3 record header is incomplete/truncated: "
                f"expected {_HEADER_SIZE} header bytes but only got {len(header)}"
            )

        # 解头；type / reserved 忽略，仅消费 payload_len
        _type, _reserved, payload_len = _HEADER_STRUCT.unpack(header)

        payload = stream.read(payload_len)
        if len(payload) < payload_len:
            # 负载长度与声明不符
            raise ValueError(
                "p3 payload length mismatch: "
                f"header declared {payload_len} bytes but only {len(payload)} available"
            )

        yield payload


def decode_opus_from_bytes_stream(input_bytes: bytes, callback: Callable[[bytes], None]) -> None:
    """解码 p3 二进制流，每解出一个 opus 包调用一次 callback（按记录顺序）。"""
    stream = io.BytesIO(input_bytes)
    for packet in _iter_p3_packets(stream):
        callback(packet)


def decode_opus_from_file_stream(input_file, callback: Callable[[bytes], None]) -> None:
    """解码 p3 文件（以二进制只读打开），每解出一个 opus 包调用一次 callback。"""
    with open(input_file, "rb") as stream:
        for packet in _iter_p3_packets(stream):
            callback(packet)


def decode_opus_from_bytes(input_bytes: bytes) -> List[bytes]:
    """解码 p3 二进制，一次性返回 opus 包列表（按记录顺序）。"""
    stream = io.BytesIO(input_bytes)
    return list(_iter_p3_packets(stream))


def decode_opus_from_file(input_file) -> List[bytes]:
    """解码 p3 文件，一次性返回 opus 包列表。"""
    with open(input_file, "rb") as stream:
        return list(_iter_p3_packets(stream))
