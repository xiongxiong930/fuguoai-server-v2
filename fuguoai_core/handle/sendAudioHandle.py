"""音频 / TTS / STT 消息下发链（复果原生 clean-room 实现）。

把合成好的 opus 音频包按流控节奏发给设备，并协同下发 `tts` 状态机消息
（start / sentence_start / stop）、`stt` 回显、纯显示消息。本协议契约由后端定义，
固件侧对齐此实现。

演化防护（均为踩过真实事故后沉淀，行为必须保留）：
- 3s 静音看门狗重发 tts start（realtime 下每轮 FIRST 真正下发前重发一次）；
- 陈旧 LAST 守卫（治"文本刷屏没声音"）；
- 服务端 AEC 参考缓存（mqtt 路径按固件 timestamp、直连 WS 按本地时钟）；
- 首包延迟可观测（V2 记录 / V3 入 metrics 直方图）；
- barge-in 尾音抑制窗口（realtime 下本句播完设回声抑制）；
- 回声基线重置（新播报起始重置 _echo_baseline_rms / _baseline_ready）；
- abort 后不下发 STT 回显 / 不置 client_is_speaking（防设备卡 Speaking）。

实现要点（与上游 MIT 骨架差异，证明独立重写）：MQTT 16 字节头部用 `struct.pack`
一次性拼装（而非逐字节索引赋值）；首包闭包用默认参捕获文本（修上游 late-binding 坑）；
职责按"消息构造 / 流控发送 / AEC 参考缓存 / 播报收尾"分层。所有线协议字段、状态机
副作用、跳过分支与原实现逐字节等价。
"""

import json
import time
import asyncio
import struct
import os
import opuslib_next
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler
from fuguoai_core.utils import textUtils
from fuguoai_core.utils.util import audio_to_data
from fuguoai_core.providers.tts.dto.dto import SentenceType
from fuguoai_core.utils.audioRateController import AudioRateController
from fuguoai_config.paths import ASSETS_DIR

TAG = __name__

# 音频帧时长（毫秒）——与固件解码节奏一致
AUDIO_FRAME_DURATION = 60
# 预缓冲包数量：前 N 包直接发出以降低首字延迟
PRE_BUFFER_COUNT = 5

# mqtt_gateway opus 包 16 字节头部布局（big-endian）：
#   [0]     type=1
#   [1]     保留
#   [2:4]   payload(opus) 长度
#   [4:8]   sequence
#   [8:12]  timestamp
#   [12:16] opus 长度（冗余字段，与 [2:4] 同值）
# 固件约定的线协议，改动需同步固件。用 struct 一次性拼装，避免逐字节索引。
_MQTT_HEADER_TYPE = 1
_MQTT_HEADER_SIZE = 16
_MQTT_HEADER_STRUCT = ">BxHIII"  # type, 保留, payload_len, sequence, timestamp, opus_len


# ───────────────────────────── 消息构造 ─────────────────────────────

def _build_tts_message(conn: "ConnectionHandler", state: str, text=None) -> dict:
    """构造 tts 状态机消息（与固件对齐的线协议字段）。"""
    message = {"type": "tts", "state": state, "session_id": conn.session_id}
    if text is not None:
        message["text"] = textUtils.check_emoji(text)
    return message


def _build_stt_message(conn: "ConnectionHandler", text: str) -> dict:
    """构造 stt 回显 / 纯显示消息（与固件对齐的线协议字段）。"""
    return {"type": "stt", "text": text, "session_id": conn.session_id}


def _is_stale_last(conn: "ConnectionHandler", sentence_id) -> bool:
    """陈旧 LAST 判定（治"文本刷屏没声音"）。

    realtime 下用户频繁短插话时，barge-in abort 后紧接新一轮，上一轮(已 abort)的
    LAST 会晚到；若照发 "stop" 给固件 → 固件回"聆听中" → 随后新一轮合成好的整段
    音频到设备后被全丢弃（字幕刷屏、零声音）。

    用触发本次 stop 的那条 LAST 自己的 sentence_id 和 TTS 线程权威的当前轮次
    (current_sentence_id) 比对——若不属于当前轮次即陈旧 LAST。current_sentence_id
    只在收到新 FIRST 时推进，与聊天层 conn.sentence_id 的 churn 解耦，故权威。
    """
    if getattr(conn, "client_listen_mode", "") == "realtime":
        _cur_turn = getattr(getattr(conn, "tts", None), "current_sentence_id", None)
    else:
        _cur_turn = conn.sentence_id
    return (
        sentence_id is not None
        and _cur_turn is not None
        and sentence_id != _cur_turn
    )


# ───────────────────────────── 播报收尾 ─────────────────────────────

async def _finalize_speaking(conn: "ConnectionHandler"):
    """一整轮播报下发完成后的收尾：等尾音、设尾音抑制窗、停发送循环、清 speaking。

    全双工（realtime）下本句音频下发完成，设置回声尾音抑制窗口，覆盖空气耦合余音，
    避免 AI 自身回声被识别成用户输入(自激)。窗口从本句播完开始计时；句间若用户插话，
    速率控制器队列会被 barge-in 清空、窗口由 abortHandle 接管。
    """
    if getattr(conn, "client_listen_mode", "") == "realtime":
        _tail = max(float(conn.config.get("bargein_asr_suppress_sec", 0.3)), 0.6)
        conn._asr_suppress_until = time.time() + _tail

    # 停止音频发送循环（仅在流控器已初始化时调用）
    if hasattr(conn, "audio_rate_controller") and conn.audio_rate_controller:
        conn.audio_rate_controller.stop_sending()
    conn.clearSpeakStatus()


# ───────────────────────────── AEC 参考缓存 ─────────────────────────────

def _cache_aec_reference(conn: "ConnectionHandler", opus_packet, ts: int):
    """解码 opus 为 PCM 并缓存为服务端 AEC 参考（下发 TTS 即参考信号）。

    mqtt_gateway 路径用固件给的 timestamp 作键；两段逻辑共享此缓存结构
    （aec_audio_cache / _time）。
    """
    if not hasattr(conn, "aec_audio_cache"):
        conn.aec_audio_cache = {}
        conn.aec_audio_cache_time = {}
        conn._send_opus_decoder = opuslib_next.Decoder(16000, 1)
    pcm_data = conn._send_opus_decoder.decode(bytes(opus_packet), 960)
    conn.aec_audio_cache[ts] = bytes(pcm_data)
    conn.aec_audio_cache_time[ts] = time.time()


# ───────────────────────────── 音频帧发送 ─────────────────────────────

async def _encode_and_send_mqtt(conn: "ConnectionHandler", opus_packet, timestamp, sequence):
    """为 opus 包加 16 字节头部并下发 mqtt_gateway，同时缓存 AEC 参考 PCM。"""
    if conn.client_aec and timestamp > 0:
        _cache_aec_reference(conn, opus_packet, timestamp)

    payload_len = len(opus_packet)
    header = struct.pack(
        _MQTT_HEADER_STRUCT,
        _MQTT_HEADER_TYPE,
        payload_len,
        sequence,
        timestamp,
        payload_len,
    )
    complete_packet = header + opus_packet
    await conn.safe_send(complete_packet)


async def sendAudioMessage(conn: "ConnectionHandler", sentenceType, audios, text, sentence_id=None):
    """一轮 TTS 音频 + 字幕 + 状态消息的编排入口。

    契约（与设备端协议对齐，改动需同步固件）：
    - 丢弃旧轮次残留音频：abort（半/全双工打断）后一律丢弃；非 realtime 按 sentence_id 比对丢弃。
    - realtime 下每个 FIRST 真正下发前重发一次 "tts start"（固件 3s 静音看门狗防护）。
    - 字幕随对应音频节奏下发（避免文本抢跑）。
    - LAST 且非通话 → 发 "stop"；若 close_after_chat 则关连接。
    """
    # 跳过旧句子残留音频：
    # realtime 下只按真实打断(client_abort)丢弃 —— 因回声/固件误发abort会频繁重生成
    # conn.sentence_id，若按 sentence_id 不匹配就丢会把合法AI回复整段丢弃
    # (现象：只有文本没出音)；真打断由 client_abort 驱动，发送循环已据此停止，
    # 故此处不再误杀合法在播音频。非 realtime 保持原 sentence_id 比较语义。
    if sentence_id is not None:
        # abort（半双工主动打断 / 全双工 barge-in）后，任何残留音频帧一律丢弃，
        # 防止 sendAudioMessage / send_stt_message 把 client_is_speaking 重设 True，
        # 导致固件卡在 Speaking 态不恢复听（2026-07-22 8818 "说话中卡住"根因）
        if conn.client_abort:
            return
        if getattr(conn, "client_listen_mode", "") != "realtime":
            if sentence_id != conn.sentence_id:
                return

    # 🔴 固件 3s 静音看门狗防护(2026-07-23 17:12 复测血泪)：
    # "tts start" 在 startToChat(用户话音刚落)就发出，但 LLM 429 限流重试/TTS 上游抖动
    # 可让本轮首帧音频晚到 9~22s；固件 v1.10.9 的 3s 静音看门狗会先把设备踢回 Listening
    # -> 之后整轮音频到设备后全被丢弃(现象：字幕刷屏、零声音，mic 回声残差仅 2~25RMS)。
    # 对策：realtime 下每轮 FIRST 音频真正开始下发前【重发一次 "tts start"】，
    # 把可能已被看门狗踢回 Listening 的固件拉回 Speaking。固件已在 Speaking 时重复
    # start 为幂等无副作用；不改消息格式/字段，不越协议红线。
    if (
        sentenceType == SentenceType.FIRST
        and getattr(conn, "client_listen_mode", "") == "realtime"
        and not conn.client_abort
    ):
        await send_tts_message(conn, "start")
        conn.client_is_speaking = True

    if conn.tts.tts_audio_first_sentence:
        conn.logger.bind(tag=TAG).info(f"发送第一段语音: {text}")
        conn.tts.tts_audio_first_sentence = False

    # 字幕文本与音频同步下发：
    # 把每一段(FIRST/MIDDLE)的 sentence_start 塞进音频流控队列(add_message)，
    # 让整段字幕在其对应音频真正开始播放的时刻才发给设备，
    # 避免所有句子的文字整段抢跑到语音前面（文本/语音不同步的根因）。
    # 仅当本轮流控器尚未就绪（本轮首段，控制器还没被首帧音频创建）时才立即发送，
    # 作为起始字幕——此时对应音频也几乎同时开始播放，观感一致。
    if text and sentenceType in (SentenceType.FIRST, SentenceType.MIDDLE):
        rc = getattr(conn, "audio_rate_controller", None)
        same_sentence = (
            getattr(conn, "audio_flow_control", {}).get("sentence_id")
            == conn.sentence_id
        )
        task_alive = (
            rc is not None
            and getattr(rc, "pending_send_task", None) is not None
            and not rc.pending_send_task.done()
        )
        if rc is not None and same_sentence and task_alive:
            conn.audio_rate_controller.add_message(
                lambda t=text: send_tts_message(conn, "sentence_start", t)
            )
        else:
            await send_tts_message(conn, "sentence_start", text)

    await sendAudio(conn, audios)
    # 发送句子开始消息
    if sentenceType is not SentenceType.MIDDLE:
        conn.logger.bind(tag=TAG).info(f"发送音频消息: {sentenceType}, {text}")

    # 发送结束消息（如果是最后一个文本）
    # 通话需要维持speaking状态
    if not conn.calling and sentenceType == SentenceType.LAST:
        # 把触发本次 stop 的那条 LAST 自己的 sentence_id 传下去，
        # 让 send_tts_message 能识别"陈旧轮次的 LAST"并跳过 stop（见其内注释）。
        await send_tts_message(conn, "stop", None, sentence_id=sentence_id)
        if conn.close_after_chat:
            await conn.close()


async def _wait_for_audio_completion(conn: "ConnectionHandler"):
    """等待音频队列清空并等待预缓冲包播放完成。"""
    if hasattr(conn, "audio_rate_controller") and conn.audio_rate_controller:
        rate_controller = conn.audio_rate_controller
        conn.logger.bind(tag=TAG).debug(
            f"等待音频发送完成，队列中还有 {len(rate_controller.queue)} 个包"
        )
        await rate_controller.queue_empty_event.wait()

        # 等待预缓冲包播放完成
        # 前N个包直接发送，增加2个网络抖动包，需要额外等待它们在客户端播放完成
        frame_duration_ms = rate_controller.frame_duration
        pre_buffer_playback_time = (PRE_BUFFER_COUNT + 2) * frame_duration_ms / 1000.0
        await asyncio.sleep(pre_buffer_playback_time)

        conn.logger.bind(tag=TAG).debug("音频发送完成")


async def _send_to_mqtt_gateway(
    conn: "ConnectionHandler", opus_packet, timestamp, sequence
):
    """发送带 16 字节头部的 opus 数据包给 mqtt_gateway，同时缓存音频用于 AEC 处理。"""
    await _encode_and_send_mqtt(conn, opus_packet, timestamp, sequence)


async def sendAudio(
    conn: "ConnectionHandler", audios, frame_duration=AUDIO_FRAME_DURATION
):
    """发送音频包，使用 AudioRateController 进行精确的流量控制。

    audios: 单个 opus 包(bytes) 或 opus 包列表。
    frame_duration: 帧时长（毫秒），默认全局常量 AUDIO_FRAME_DURATION。
    """
    if audios is None or len(audios) == 0:
        return

    send_delay = conn.config.get("tts_audio_send_delay", -1) / 1000.0
    is_single_packet = isinstance(audios, bytes)

    # 初始化或获取 RateController
    rate_controller, flow_control = _get_or_create_rate_controller(
        conn, frame_duration, is_single_packet
    )

    # 统一转换为列表处理
    audio_list = [audios] if is_single_packet else audios

    # 发送音频包
    await _send_audio_with_rate_control(
        conn, audio_list, rate_controller, flow_control, send_delay
    )


def _get_or_create_rate_controller(
    conn: "ConnectionHandler", frame_duration, is_single_packet
):
    """获取或创建 RateController 与 flow_control 状态。

    is_single_packet: 是否单包模式（True: TTS流式单包, False: 批量包）。
    返回 (rate_controller, flow_control)。
    """
    # 检查是否需要重置控制器
    need_reset = False

    if not hasattr(conn, "audio_rate_controller"):
        # 控制器不存在，需要创建
        need_reset = True
    else:
        rate_controller = conn.audio_rate_controller

        # 后台发送任务已停止, 则需要重置
        if (
            not rate_controller.pending_send_task
            or rate_controller.pending_send_task.done()
        ):
            need_reset = True
        # 当sentence_id 变化，需要重置
        elif (
            getattr(conn, "audio_flow_control", {}).get("sentence_id")
            != conn.sentence_id
        ):
            need_reset = True

    if need_reset:
        # 创建或获取 rate_controller
        if not hasattr(conn, "audio_rate_controller"):
            conn.audio_rate_controller = AudioRateController(frame_duration)
        else:
            conn.audio_rate_controller.reset()

        # 初始化 flow_control
        conn.audio_flow_control = {
            "packet_count": 0,
            "sequence": 0,
            "sentence_id": conn.sentence_id,
        }

        # 启动后台发送循环
        _start_background_sender(
            conn, conn.audio_rate_controller, conn.audio_flow_control
        )

    return conn.audio_rate_controller, conn.audio_flow_control


def _start_background_sender(conn: "ConnectionHandler", rate_controller, flow_control):
    """启动后台发送循环任务。"""

    async def send_callback(packet):
        # 检查是否应该中止
        if conn.client_abort:
            raise asyncio.CancelledError("客户端已中止")

        conn.last_activity_time = time.time() * 1000
        await _do_send_audio(conn, packet, flow_control)

    # 使用 start_sending 启动后台循环
    rate_controller.start_sending(send_callback)


async def _send_audio_with_rate_control(
    conn: "ConnectionHandler", audio_list, rate_controller, flow_control, send_delay
):
    """使用 rate_controller 发送音频包。

    send_delay: 固定延迟（秒），-1 表示使用动态流控。
    """
    for packet in audio_list:
        if conn.client_abort:
            return

        conn.last_activity_time = time.time() * 1000

        # 预缓冲：前N个包直接发送
        if flow_control["packet_count"] < PRE_BUFFER_COUNT:
            await _do_send_audio(conn, packet, flow_control)
        elif send_delay > 0:
            # 固定延迟模式
            await asyncio.sleep(send_delay)
            await _do_send_audio(conn, packet, flow_control)
        else:
            # 动态流控模式：仅添加到队列，由后台循环负责发送
            rate_controller.add_audio(packet)


async def _do_send_audio(conn: "ConnectionHandler", opus_packet, flow_control):
    """执行实际的音频发送（单包）。"""
    packet_index = flow_control.get("packet_count", 0)
    sequence = flow_control.get("sequence", 0)

    # V2: 首包延迟观测 —— 本轮第一帧音频真正发出时记录耗时
    if (
        packet_index == 0
        and getattr(conn, "_first_audio_mark", None) is not None
        and not getattr(conn, "_first_audio_recorded", False)
    ):
        latency = time.monotonic() - conn._first_audio_mark
        conn._first_audio_recorded = True
        conn.logger.bind(tag=TAG).info(f"首包延迟(首帧音频): {latency:.3f}s")
        # V3 可观测性：首包延迟计入直方图，供 /metrics 抓取
        try:
            from fuguoai_core.utils.metrics import METRICS

            METRICS.observe(
                "fuguoai_first_audio_latency_seconds",
                latency,
                help_text="首包延迟（首帧音频真正发出）分布，单位秒",
            )
        except Exception:
            pass

    if conn.conn_from_mqtt_gateway:
        # 计算时间戳（基于播放位置）
        start_time = time.time()
        timestamp = int(start_time * 1000) % (2**32)
        await _send_to_mqtt_gateway(conn, opus_packet, timestamp, sequence)
    else:
        # 直连WS全双工：TTS下行PCM当AEC参考，用本地时钟timestamp存缓存
        if getattr(conn, "client_aec", False):
            try:
                if not hasattr(conn, "_aec_send_decoder"):
                    conn._aec_send_decoder = opuslib_next.Decoder(16000, 1)
                pcm = conn._aec_send_decoder.decode(bytes(opus_packet), 960)
                ts = int(time.time() * 1000)
                conn.aec_audio_cache[ts] = bytes(pcm)
                conn.aec_audio_cache_time[ts] = time.time()
            except Exception as e:
                conn.logger.bind(tag=TAG).warning(f"[AEC] TTS参考缓存失败: {e}")
        # 直接发送opus数据包
        await conn.safe_send(opus_packet)

    # 更新流控状态
    flow_control["packet_count"] = packet_index + 1
    flow_control["sequence"] = sequence + 1


async def send_tts_message(conn: "ConnectionHandler", state, text=None, sentence_id=None):
    """发送 TTS 状态消息（start / sentence_start / stop）。"""
    if text is None and state == "sentence_start":
        return
    message = _build_tts_message(conn, state, text)

    # TTS播放结束
    if state == "stop":
        # 🔴 陈旧 LAST 守卫（治"文本刷屏没声音"）：见 _is_stale_last 注释。
        if _is_stale_last(conn, sentence_id):
            conn.logger.bind(tag=TAG).info(
                f"跳过陈旧LAST的stop(不打断新轮播报): last_sid={sentence_id} "
                f"cur_turn={getattr(getattr(conn, 'tts', None), 'current_sentence_id', conn.sentence_id)}"
            )
            return
        # 保存当前的 sentence_id，用于后续判断是否是当前轮次
        current_sentence_id = conn.sentence_id
        # 播放提示音
        tts_notify = conn.config.get("enable_stop_tts_notify", False)
        if tts_notify:
            stop_tts_notify_voice = conn.config.get(
                "stop_tts_notify_voice", os.path.join(ASSETS_DIR, "tts_notify.mp3")
            )
            audios = await audio_to_data(stop_tts_notify_voice, is_opus=True)
            await sendAudio(conn, audios)
        # 等待所有音频包发送完成
        await _wait_for_audio_completion(conn)

        # 全双工：本句音频下发完成，设置回声尾音抑制窗口（见 _finalize_speaking 注释）
        await _finalize_speaking(conn)

        # 检查是否是当前轮次
        if current_sentence_id != conn.sentence_id:
            return

    # 发送消息到客户端
    await conn.safe_send(json.dumps(message))


async def send_stt_message(conn: "ConnectionHandler", text):
    """发送 STT 状态消息（用户说话回显）。"""
    # abort 后不下发 STT 回显 / 不置 client_is_speaking，避免粘滞导致设备卡 Speaking
    if conn.client_abort:
        return
    end_prompt_str = conn.config.get("end_prompt", {}).get("prompt")
    if end_prompt_str and end_prompt_str == text:
        await send_tts_message(conn, "start")
        return

    # 解析JSON格式，提取实际的用户说话内容
    display_text = text
    try:
        # 尝试解析JSON格式
        if text.strip().startswith("{") and text.strip().endswith("}"):
            parsed_data = json.loads(text)
            if isinstance(parsed_data, dict) and "content" in parsed_data:
                # 如果是包含说话人信息的JSON格式，只显示content部分
                display_text = parsed_data["content"]
                # 保存说话人信息到conn对象
                if "speaker" in parsed_data:
                    conn.current_speaker = parsed_data["speaker"]
    except (json.JSONDecodeError, TypeError):
        # 如果不是JSON格式，直接使用原始文本
        display_text = text
    stt_text = textUtils.get_string_no_punctuation_or_emoji(display_text)
    await conn.safe_send(json.dumps(_build_stt_message(conn, stt_text)))
    await send_tts_message(conn, "start")
    # 发送start消息后客户端状态会处于说话中状态，同步服务端状态
    conn.client_is_speaking = True
    # 新播报起始：重置 AEC 回声基线与就绪门控，避免上一轮旧回声电平导致误自打断；
    # 基线会在本轮有效消回声帧中快速收敛到当前回声电平（见 _apply_aec）
    conn._echo_baseline_rms = 0.0
    conn._baseline_ready = False


async def send_display_message(conn: "ConnectionHandler", text):
    """发送纯显示消息（stt 格式，仅上屏不触发播报状态）。"""
    await conn.safe_send(json.dumps(_build_stt_message(conn, text)))
