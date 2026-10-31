"""p3 容器解码（复果自有实现）。

p3 是我们用于承载 opus 包的极简容器格式，逐条记录拼接而成：

    记录 = 4 字节头 + 负载
    头  ：大端 '>BBH'，依次为 (类型 1B, 保留 1B, 负载长度 2B)
    负载：一个完整的 opus 包

对外同时提供两种形态：
  * 流式：逐包回吐给 callback，供 TTS 边解边推流；
  * 一次性：直接返回包列表，供需要整体拿到的场景。
"""

import io
import struct
from typing import Callable, Iterator, List

_P3_RECORD_HEADER = struct.Struct(">BBH")


def _iter_p3_packets(stream) -> Iterator[bytes]:
    """在二进制流上逐条解析 p3 记录，产出每个 opus 包。

    头部或负载被截断时抛 ValueError，避免把半包数据当作完整帧推给下游。
    """
    while True:
        header = stream.read(_P3_RECORD_HEADER.size)
        if not header:
            return
        if len(header) < _P3_RECORD_HEADER.size:
            raise ValueError("p3: 记录头不完整，文件可能已截断")

        _, _, payload_len = _P3_RECORD_HEADER.unpack(header)
        payload = stream.read(payload_len)
        if len(payload) != payload_len:
            raise ValueError(
                f"p3: 负载长度({len(payload)})与头部声明({payload_len})不符"
            )
        yield payload


def decode_opus_from_bytes_stream(
    input_bytes: bytes, callback: Callable[[bytes], None]
) -> None:
    """解码 p3 二进制，每解出一个 opus 包就调用一次 callback。"""
    for packet in _iter_p3_packets(io.BytesIO(input_bytes)):
        callback(packet)


def decode_opus_from_file_stream(
    input_file, callback: Callable[[bytes], None]
) -> None:
    """解码 p3 文件，每解出一个 opus 包就调用一次 callback。"""
    with open(input_file, "rb") as stream:
        for packet in _iter_p3_packets(stream):
            callback(packet)


def decode_opus_from_bytes(input_bytes: bytes) -> List[bytes]:
    """解码 p3 二进制，一次性返回 opus 包列表。"""
    return list(_iter_p3_packets(io.BytesIO(input_bytes)))


def decode_opus_from_file(input_file) -> List[bytes]:
    """解码 p3 文件，一次性返回 opus 包列表。"""
    with open(input_file, "rb") as stream:
        return list(_iter_p3_packets(stream))
