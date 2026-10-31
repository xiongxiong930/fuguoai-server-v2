"""FALP v1 — 复果自有通信协议（clean-room 设计，与 xiaozhi 协议无任何线格式兼容）。

本子包是 P1 后端原生协议层，完全 additive：不 import 任何 legacy 模块，
不修改 connection.py / websocket_server.py 等既有代码。legacy 路径保持零改动。

模块：
  envelope   — fg.* 文本帧信封（JSON 消息类型）
  audio_frame— FALP 二进制音频帧（magic=FALP + v1 结构）
  handshake  — FALP-Auth：HMAC-SHA256 挑战-响应（防重放）
  canonical  — 与线格式无关的内部领域事件（adapter 的桥）
  adapter    — fg.* <-> canonical 映射
"""
from . import envelope, audio_frame, handshake, canonical, adapter

__all__ = ["envelope", "audio_frame", "handshake", "canonical", "adapter"]
