"""FALP v1 会话状态机（P1 原生链路核心）。additive，零 legacy import。

职责：
  - 按握手状态机收 fg.* 文本帧 / FALP 二进制帧；
  - 把设备上行转成 canonical 领域事件，交给业务管线（on_event）；
  - 把业务管线产出的 canonical 事件（pipeline_out）转回 fg.* 下行（emit）。

legacy(xiaozhi) 路径完全不经过此处。本模块只做协议编排，不含 ASR/LLM/TTS 逻辑
（那些在业务管线里，原生层与之通过 canonical 解耦）。
"""
from __future__ import annotations

import json
import uuid
from typing import Callable, Optional

from . import envelope, audio_frame, handshake, canonical, adapter

# 会话状态
HELLO, AUTH, OPEN, CLOSED = 0, 1, 2, 3

# 回调类型
Emitter = Callable[[envelope.FalpMessage], None]          # 下行 fg.* 帧
EventHandler = Callable[[object], None]                   # 上行 canonical 事件
SecretResolver = Callable[[str], str]                     # device_id -> device_secret


class FalpSession:
    def __init__(self, authority: handshake.HandshakeAuthority,
                 emit: Emitter, on_event: EventHandler, secret_of: SecretResolver):
        self._auth = authority
        self._emit = emit
        self._on_event = on_event
        self._secret_of = secret_of
        self.state = HELLO
        self.device_id: Optional[str] = None
        self.session_id: Optional[str] = None
        self._nonce: Optional[str] = None

    # ---- 入口 ----
    def feed_text(self, raw: bytes) -> None:
        self._dispatch(envelope.FalpMessage.from_dict(json.loads(raw.decode("utf-8"))))

    def feed_binary(self, raw: bytes) -> None:
        frame = audio_frame.FalpAudioFrame.decode(raw)
        self._on_event(adapter.audio_frame_to_canonical(frame))

    # ---- 下行助手 ----
    def _deny(self, code: str, reason: str) -> None:
        self.state = CLOSED
        self._emit(envelope.FalpMessage.deny(code, reason))

    # ---- 分发 ----
    def _dispatch(self, msg: envelope.FalpMessage) -> None:
        if self.state == HELLO:
            if msg.type != "fg.hello" or msg.data.get("proto") != "falp":
                self._deny("4000", "unsupported protocol")
                return
            self.device_id = msg.data["device"]["id"]
            self._nonce, _ = self._auth.issue()
            self.state = AUTH
            self._emit(envelope.FalpMessage.challenge(self._nonce, self._auth.expires_in))
            return

        if self.state == AUTH:
            if msg.type != "fg.auth":
                self._deny("4000", "expected auth")
                return
            dev = msg.data.get("device_id", "")
            token = msg.data.get("token", "")
            if not self._auth.verify(dev, token, self._nonce, self._secret_of(self.device_id)):
                self._deny("4001", "auth_failed")
                return
            self.session_id = str(uuid.uuid4())
            self.state = OPEN
            self._emit(envelope.FalpMessage.welcome(self.session_id))
            return

        if self.state == OPEN:
            if msg.type == "fg.abort":
                self._on_event(canonical.CanonicalAbort(msg.data.get("reason", "")))
            elif msg.type == "fg.nfc":
                self._on_event(canonical.CanonicalNfc(msg.data.get("op", ""), msg.data.get("card_uid", "")))
            elif msg.type == "fg.iot":
                self._on_event(canonical.CanonicalIot(event=msg.data.get("event")))
            elif msg.type == "fg.schedule" and msg.data.get("op") == "pull":
                # 服务端推送日程（stub 暂返回空表，真实管线从存储取）
                self._emit(envelope.FalpMessage.schedule("push", tasks=[], sid=self.session_id))
            elif msg.type == "fg.ping":
                self._emit(envelope.FalpMessage.pong(sid=self.session_id))
            elif msg.type == "fg.bye":
                self.state = CLOSED
            # fg.listen 等控制消息：本 stub 不下行应答（真实管线会据此起停采集）
            return

        # CLOSED：忽略

    # ---- 业务管线产出 -> 下行 ----
    def pipeline_out(self, evt: object) -> None:
        if isinstance(evt, canonical.CanonicalAsr):
            self._emit(adapter.asr_from_canonical(evt, sid=self.session_id))
        elif isinstance(evt, canonical.CanonicalLlm):
            self._emit(adapter.llm_from_canonical(evt, sid=self.session_id))
        elif isinstance(evt, canonical.CanonicalTts):
            self._emit(adapter.tts_from_canonical(evt, sid=self.session_id))
        elif isinstance(evt, canonical.CanonicalIot):
            self._emit(adapter.iot_from_canonical(evt, sid=self.session_id))
