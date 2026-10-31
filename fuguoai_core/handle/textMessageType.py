from enum import Enum


class TextMessageType(Enum):
    """消息类型枚举。

    统一标识服务端与设备之间各类文本消息的类别，是消息路由的判别键：
    每个 ``textHandler/*MessageHandler.py`` 通过 ``message_type`` 返回本枚举成员，
    注册表据此把收到的消息派发到对应处理器。

    成员分为两组：
    * **基础消息**：握手（HELLO）、打断（ABORT）、监听（LISTEN）、设备物联（IOT）、
      模型上下文协议（MCP）、服务端主动指令（SERVER）、保活（PING）。
    * **复果扩展消息**：音频回灌参考（AUDIO_PLAYBACK_REF）、会话内唤醒词
      （WAKE_WORD_IN_SESSION）、播报请求（SPEAK_REQUEST）、云端日程拉取
      （SCHEDULE_PULL）、NFC 碰卡与领卡（NFC_TAP / NFC_CLAIM）。
    """

    HELLO = "hello"
    ABORT = "abort"
    LISTEN = "listen"
    IOT = "iot"
    MCP = "mcp"
    SERVER = "server"
    PING = "ping"
    AUDIO_PLAYBACK_REF = "audio_playback_ref"
    WAKE_WORD_IN_SESSION = "wake_word_in_session"
    SPEAK_REQUEST = "speak_request"
    SCHEDULE_PULL = "schedule_pull"  # M2 下行：设备重连拉取云端日程（契约 §5.2）
    NFC_TAP = "nfc_tap"  # 天使心：盒子 reader 经 WS 上报碰卡（source=device，模型 b）
    NFC_CLAIM = "nfc_claim"  # 天使心：盒子 reader 经 WS 上报领卡（一次性绑 owner，模型 b）

    # ── v3.0 协议类型（后端定义，固件对齐；与既有 v1/v2 类型并存，双版本兼容）──
    # 语义别名：v3.0 用更直白的单动作类型名，映射到既有处理器（向后兼容）。
    AUTH = "auth"  # v3.0 鉴权消息（与 HTTP Header 鉴权双认）
    START_LISTENING = "start_listening"  # ≡ listen{state:start}
    STOP_LISTENING = "stop_listening"  # ≡ listen{state:stop}
    WAKE_WORD_DETECTED = "wake_word_detected"  # ≡ wake_word_in_session
    ABORT_SPEAKING = "abort_speaking"  # ≡ abort
    PONG = "pong"  # 设备对服务端 ping 的应答（刷新活跃）
    # v3.0 新增、无 v1/v2 等价物：
    SCHEDULE_PUSH = "schedule_push"  # 设备→云端全量镜像（M1）
    SCHEDULE_MIRROR = "schedule_mirror"  # 设备请求云端以设备为镜像源（≡ push）
    INTERCOM_READY = "intercom_ready"  # 对讲链路就绪确认
    CLOCK = "clock"  # 时钟同步：设备请求/上报，服务端回 server_time
