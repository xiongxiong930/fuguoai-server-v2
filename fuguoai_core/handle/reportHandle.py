"""
复果聊天记录上报（clean-room 重写实现）。

设计：上报逻辑下沉到 `ConnectionHandler`（连接对象持有独立上报队列与消费线程，
生命周期随连接绑定）。本模块只提供「入队 / 格式转换」辅助函数，供 connection 在
对话各阶段（用户 ASR、智能体 TTS、工具调用）调用，不直接触碰传输细节。

线协议（与管理后台约定，不可改）：
- chat_type: 1=用户语音 / 2=智能体语音 / 3=工具调用
- chat_history_conf: 0=关闭上报；2=携带音频；其余=仅文本
- 队列元素统一为四元组 (chat_type, text, audio_or_None, ms_timestamp)
- 工具调用文案格式固定为 `name({json})`，工具结果固定为 `{"result":"..."}`
"""

import time
import json
import io
import wave
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler

from fuguoai_config.manage_api_client import report as manage_report

TAG = __name__

# ---- 音频格式常量（原魔术数提取，便于一处维护）----
_WAV_SAMPLE_RATE = 16000
_WAV_CHANNELS = 1
_WAV_SAMPLE_WIDTH = 2  # 16-bit
_WAV_BYTE_RATE = _WAV_SAMPLE_RATE * _WAV_CHANNELS * _WAV_SAMPLE_WIDTH

_CHAT_TYPE_USER = 1
_CHAT_TYPE_AGENT = 2
_CHAT_TYPE_TOOL = 3

# 工具调用 / 结果文案模板（管理后台解析契约，固定格式）
_TOOL_CALL_TEMPLATE = "{name}({args})"
_TOOL_RESULT_TEMPLATE = '{{"result":"{result}"}}'


def _now_ms() -> int:
    """毫秒时间戳（队列元素末尾用）。"""
    return int(time.time() * 1000)


def _enqueue_allowed(conn: "ConnectionHandler", enable_attr: str = None) -> bool:
    """是否允许入队：未从 API 拉配置 / 需绑定 / 对应开关关闭 → 跳过。

    enable_attr 为 None 时不做开关判定（工具调用无独立开关）。
    """
    if not conn.read_config_from_api or conn.need_bind:
        return False
    if enable_attr is not None and not getattr(conn, enable_attr):
        return False
    return True


def _pcm_to_wav_bytes(pcm_data) -> bytes:
    """16-bit PCM 字节（或字节列表）封装为 16k/单声道 WAV 字节流。

    用标准库 `wave` 编码而非手写 RIFF 头，路径不同但产出的字节与协议一致。
    """
    pcm_bytes = b"".join(pcm_data) if isinstance(pcm_data, (list, tuple)) else pcm_data
    if not pcm_bytes:
        raise ValueError("没有有效的PCM数据")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(_WAV_CHANNELS)
        wf.setsampwidth(_WAV_SAMPLE_WIDTH)
        wf.setframerate(_WAV_SAMPLE_RATE)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def opus_to_wav(conn: "ConnectionHandler", pcm_data):
    """把 16-bit PCM 字节（或字节列表）封装为 WAV 格式字节流（16k/单声道）。

    对外契约名保留；内部实现委托 `_pcm_to_wav_bytes`，编码失败记日志并上抛。
    """
    try:
        return _pcm_to_wav_bytes(pcm_data)
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"PCM转WAV失败: {e}", exc_info=True)
        raise


async def report(conn: "ConnectionHandler", type, text, opus_data, report_time):
    """执行聊天记录上报：先把 opus 音频转 WAV，再异步推送至管理后台。"""
    try:
        audio_data = opus_to_wav(conn, opus_data) if opus_data else None
        await manage_report(
            mac_address=conn.device_id,
            session_id=conn.session_id,
            chat_type=type,
            content=text,
            audio=audio_data,
            report_time=report_time,
        )
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"聊天记录上报失败: {e}")


def enqueue_tts_report(conn: "ConnectionHandler", text, opus_data):
    """把智能体 TTS 结果加入上报队列（受 chat_history_conf / 开关控制）。"""
    if not _enqueue_allowed(conn, "report_tts_enable"):
        return
    if conn.chat_history_conf == 0:
        return
    try:
        ts = _now_ms()
        if conn.chat_history_conf == 2:
            conn.report_queue.put((_CHAT_TYPE_AGENT, text, opus_data, ts))
        else:
            conn.report_queue.put((_CHAT_TYPE_AGENT, text, None, ts))
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"加入TTS上报队列失败: {text}, {e}")


def enqueue_tool_report(
    conn: "ConnectionHandler",
    tool_name: str,
    tool_input: dict,
    tool_result: str = None,
    report_tool_call: bool = True,
):
    """把工具调用（及可选结果）加入上报队列。"""
    if not _enqueue_allowed(conn):
        return
    if conn.chat_history_conf == 0:
        return
    try:
        ts = _now_ms()
        if report_tool_call:
            call_text = json.dumps(
                [
                    {
                        "type": "tool",
                        "text": _TOOL_CALL_TEMPLATE.format(
                            name=tool_name,
                            args=json.dumps(tool_input, ensure_ascii=False),
                        ),
                    }
                ]
            )
            conn.report_queue.put((_CHAT_TYPE_TOOL, call_text, None, ts))
        if tool_result:
            result_content = json.dumps(
                [
                    {
                        "type": "tool_result",
                        "text": _TOOL_RESULT_TEMPLATE.format(result=str(tool_result)),
                    }
                ],
                ensure_ascii=False,
            )
            conn.report_queue.put((_CHAT_TYPE_TOOL, result_content, None, ts + 1))
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"加入工具上报队列失败: {e}")


def enqueue_asr_report(conn: "ConnectionHandler", text, opus_data):
    """把用户 ASR 结果加入上报队列（受 chat_history_conf / 开关控制）。"""
    if not _enqueue_allowed(conn, "report_asr_enable"):
        return
    if conn.chat_history_conf == 0:
        return
    try:
        ts = _now_ms()
        if conn.chat_history_conf == 2:
            conn.report_queue.put((_CHAT_TYPE_USER, text, opus_data, ts))
        else:
            conn.report_queue.put((_CHAT_TYPE_USER, text, None, ts))
    except Exception as e:
        conn.logger.bind(tag=TAG).error(f"加入ASR上报队列失败: {text}, {e}")
