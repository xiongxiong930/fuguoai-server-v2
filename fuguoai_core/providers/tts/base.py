"""语音合成（TTS）提供方基类 —— 复果 AI 自有实现（clean-room 重写）。

固定「文本 → 分段 → 流式/一次性合成 → opus 编码 → 下发」的主链路，所有 provider
（腾讯云 / Edge / CosyVoice / 兜底）共用。下列均为复果在线上踩坑后沉淀的防护，非上游逻辑：

- 常驻事件循环：每个合成线程持有一个 thread-local loop，替代每段 `asyncio.run()` 的建/毁
  开销（正是 12s 冷启动尖峰的诱因之一），兜底 provider 在同一线程天然共享该 loop；
- 增量拼接 O(n)：分段抽取只拼新增块，不再每次全量 ``"".join(buff)``（长回答下 O(n²)）；
- V2 句内预刷新窗口：长句/无标点长串在缓冲超阈值时提前合成前一段，削首包延迟；
- V3 TTS 熔断器 + 会话内锁定兜底：主后端劣化时整连接统一走兜底后端，避免逐句拼音色；
- 合成失败秒释 speaking（`_release_speaking_on_failure`），避免设备卡"说话中"无声音；
- `_estimate_send_timeout` 按音频时长动态估算下发超时，纠正固定 5s 对长回答的结构性虚警；
- realtime 模式用 `current_sentence_id` 过滤旧句，规避固件误发 abort 导致的合法回复被丢；
- 流式替换词滑动窗口匹配，处理跨分片的纠错词。

线协议与 xiaozhi-esp32 固件对齐（固件侧协议随后端演进）。公共方法名/属性名为活契约
（connection / intentHandler / sendAudioHandle 及子类依赖），重写保持签名不变。
"""

import os
import re
import time
import uuid
import queue
import asyncio
import threading
import traceback
import concurrent.futures
from datetime import datetime
from typing import Callable, Any
from abc import ABC, abstractmethod

from fuguoai_config.logger import setup_logging
from fuguoai_core.utils import p3, textUtils, opus_encoder_utils
from fuguoai_core.utils.tts import MarkdownCleaner, convert_percentage_to_range
from fuguoai_core.utils.output_counter import add_device_output
from fuguoai_core.utils.util import audio_bytes_to_data_stream, audio_to_data_stream
from fuguoai_core.utils.metrics import METRICS
from fuguoai_core.utils.circuit_breaker import get_breaker, CircuitOpenError
from fuguoai_core.utils.asyncio_util import run_coroutine_threadsafe_with_timeout
from fuguoai_core.handle.reportHandle import enqueue_tts_report
from fuguoai_core.handle.sendAudioHandle import sendAudioMessage, send_tts_message
from fuguoai_core.providers.tts.dto.dto import (
    TTSMessageDTO,
    SentenceType,
    ContentType,
    InterfaceType,
)

TAG = __name__
logger = setup_logging()

# ==========================================================================
# 常驻事件循环（复果演进）：每个合成线程一个 thread-local loop，语义等同 asyncio.run
# ==========================================================================
_thread_loop = threading.local()


def _run_on_thread_loop(coro):
    """在当前线程的常驻事件循环上同步跑协程，替代每段 asyncio.run 的建/毁开销。"""
    loop = getattr(_thread_loop, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        _thread_loop.loop = loop
    return loop.run_until_complete(coro)


# websockets 连接关闭异常：默认 __str__ 不含 "closed"/"websocket" 关键字，纯关键字匹配
# 会漏判正常断开 → 误记 ERROR 并让播放线程空转。故优先按异常类型识别（不受版本文案影响）。
try:
    from websockets.exceptions import (
        ConnectionClosed,
        ConnectionClosedError,
        ConnectionClosedOK,
    )
    _WS_CLOSED = (ConnectionClosed, ConnectionClosedOK, ConnectionClosedError)
except Exception:  # pragma: no cover - 极老 websockets 兜底
    _WS_CLOSED = ()

# 句末强边界（首句与非首句共用同一集合，保持行为一致）。
_PUNCTUATION_SET = ("。", "？", "?", "！", "!", "；", ";", "：")
# 首句弱边界：逗号/顿号/分号/冒号，用于首包提前 flush。
_FIRST_WEAK_BOUNDARY = "，、；："
# 纯标点/符号判定集（用于拦截无法合成的独立标点块）。
_SYMBOL_CHARS = '，。！？、；：,.;:!?…—~·"\'()（）【】[]{}《》<>/\\|@#%&*+=_-'
# V2 句内预刷新：自然停顿候选（窗口内从后往前找最靠后的停顿点）。
_REFRESH_PAUSES = "，、；：,;: \u2014\u2026—"


class TTSProviderBase(ABC):
    def __init__(self, config, delete_audio_file):
        self.interface_type = InterfaceType.NON_STREAM
        self.conn = None
        self.delete_audio_file = delete_audio_file
        self.audio_file_type = "wav"
        self.output_file = config.get("output_dir", "tmp/")
        self.tts_timeout = int(config.get("tts_timeout", 15))
        self.tts_text_queue = queue.Queue()
        self.tts_audio_queue = queue.Queue()
        self.tts_audio_first_sentence = True
        # 句内预刷新窗口：长句/无标点长串超该字数即提前合成前一段，削首包延迟。
        self.tts_segment_max_chars = int(config.get("tts_segment_max_chars", 45))
        # TTS 熔断器：连续失败 8 次开路，30s 后半开探测；开路时段快速失败避免打宕机后端。
        self._tts_breaker = get_breaker(
            "tts", failure_threshold=8, recovery_timeout=30.0, half_open_max_calls=1
        )
        self.before_stop_play_files = []
        self.report_on_last = False
        # sentence_id → 文本，供流式 TTS 取正确字幕。
        self._sentence_text_map = {}
        # 纠错词：config["correct_words"] 为 ["原|改", ...]，构建正向/反向正则 + 首字索引。
        self.correct_words = {}
        self._correct_words_pattern = None
        self._reverse_words_pattern = None
        self._reverse_words_map = None
        self._words_by_first_char = {}
        self._build_correct_words(config.get("correct_words", []))
        # 流式滑动窗口待匹配前缀。
        self._pending_prefix = ""
        self.tts_text_buff = []
        # 增量拼接缓存（O(n)）：只拼新增块，缓冲被替换/收缩时自动重建。
        self._joined_cache = ""
        self._buff_seen_count = 0
        self._buff_id = id(self.tts_text_buff)
        self._join_work_chars = 0  # 诊断：累计实际 join 字符量
        self.punctuations = _PUNCTUATION_SET
        self.first_sentence_punctuations = _PUNCTUATION_SET
        # 首句弱边界杠杆：首句在第一个逗号/顿号/分号/冒号处提前 flush，削首包延迟；
        # 弱边界过于靠前(<min)时仍等强边界，避免"嗯，"式碎段。
        self.first_comma_boundary = (
            bool(config.get("first_comma_boundary", True)) if config else True
        )
        self.first_comma_min_seg = (
            int(config.get("first_comma_min_seg", 4)) if config else 4
        )
        self.tts_stop_request = False
        self.processed_chars = 0
        self.is_first_sentence = True
        # 合成失败 UX 兜底：本回合任一段失败立即释放 speaking，避免设备卡"说话中"。
        self._tts_turn_failed = False
        self._released_tts_sentences = set()
        # 会话内锁定兜底状态机。
        self._fallback_locked = False
        self._fb_lock_count = 0
        self._FB_PROBE_EVERY = 8  # 锁定后每 8 句探活一次主后端
        # 当前轮次标记（realtime 下权威轮次，与聊天层 churn 解耦）。
        self.current_sentence_id = None

    def _build_correct_words(self, raw_items):
        """从 ["原|改", ...] 构建正向/反向替换正则与首字索引。"""
        mapping = {}
        for item in raw_items:
            parts = item.split("|", 1)
            if len(parts) == 2:
                mapping[parts[0]] = parts[1]
        self.correct_words = mapping
        if not mapping:
            return
        # 正向：按 key 长度降序，长词优先匹配，避免短词部分干扰。
        ordered_keys = sorted(mapping.keys(), key=len, reverse=True)
        self._correct_words_pattern = re.compile(
            "|".join(re.escape(k) for k in ordered_keys)
        )
        reverse_map = {v: k for k, v in mapping.items()}
        self._reverse_words_map = reverse_map
        self._reverse_words_pattern = re.compile(
            "|".join(re.escape(k) for k in sorted(reverse_map.keys(), key=len, reverse=True))
        )
        # 首字索引：按首字分组的替换词字典，用于流式滑动窗口快速查找。
        for key in ordered_keys:
            first = key[0] if key else ""
            self._words_by_first_char.setdefault(first, []).append(key)

    def generate_filename(self, extension=".wav"):
        """生成一次性 TTS 临时文件名：日期 + 随机 hex，避免会话间撞名。"""
        return os.path.join(
            self.output_file,
            f"tts-{datetime.now().date()}@{uuid.uuid4().hex}{extension}",
        )

    def handle_opus(self, opus_data: bytes):
        # 即时打断：用户插话(client_abort)时不再向播放队列推音频，实现"秒停"。
        if getattr(self.conn, "client_abort", False):
            return
        logger.bind(tag=TAG).debug(f"推送数据到队列里面帧数～～ {len(opus_data)}")
        self.tts_audio_queue.put(
            (SentenceType.MIDDLE, opus_data, None, self.current_sentence_id)
        )

    def handle_audio_file(self, file_audio: bytes, text):
        """登记停播前预置音频（提示音等），本轮结束时由 _process_before_stop_play_files 下发。"""
        self.before_stop_play_files.append((file_audio, text))

    # ----------------------------------------------------------------------
    # 流式合成主入口
    # ----------------------------------------------------------------------
    def to_tts_stream(self, text, opus_handler: Callable[[bytes], None] = None) -> None:
        raw_text = text
        text = MarkdownCleaner.clean_markdown(text)
        if self._correct_words_pattern:
            text = self._correct_words_pattern.sub(
                lambda m: self.correct_words[m.group(0)], text
            )
        retries = 5
        # 本回合已失败：后续分段直接跳过，不再阻塞/产生孤儿音频。
        if getattr(self, "_tts_turn_failed", False):
            return None
        if self.delete_audio_file:
            # 流式快路径：主用(非兜底锁定)且 provider 实现 text_to_stream 时边合成边下发。
            if (not self._should_route_fallback()) and hasattr(self, "text_to_stream"):
                try:
                    METRICS.inc("fuguoai_tts_requests_total")
                    sent_type = (
                        SentenceType.FIRST if self.tts_audio_first_sentence
                        else SentenceType.MIDDLE
                    )
                    self.tts_audio_first_sentence = False
                    self.tts_audio_queue.put(
                        (sent_type, None, raw_text, self.current_sentence_id)
                    )
                    emitted = {"any": False}

                    def _on_pcm(pcm):
                        emitted["any"] = True
                        self.opus_encoder.encode_pcm_to_opus_stream(
                            pcm, False, opus_handler
                        )

                    self._tts_breaker.call(
                        lambda: _run_on_thread_loop(self.text_to_stream(text, _on_pcm))
                    )
                    # 冲刷末帧（零填充）确保送达。
                    self.opus_encoder.encode_pcm_to_opus_stream(b"", True, opus_handler)
                    self._on_primary_ok()
                    logger.bind(tag=TAG).info(f"语音生成成功(流式): {raw_text}")
                    return None
                except CircuitOpenError:
                    # 熔断开路且未发音频 → 落下方一次性兜底。
                    pass
                except Exception as exc:  # noqa: BLE001
                    last_err = str(exc)
                    if emitted["any"]:
                        # 已发部分音频，避免与兜底重叠 → 释放 speaking 直接返回。
                        logger.bind(tag=TAG).error(
                            f"流式TTS中途失败已释放: {raw_text}，原因: {last_err}"
                        )
                        self._release_speaking_on_failure(self.conn, self.current_sentence_id)
                        return None
                    logger.bind(tag=TAG).warning(
                        f"流式TTS失败转一次性兜底: {raw_text}，原因: {last_err}"
                    )
            # 会话锁定：直接走兜底后端，避免逐句拼音色。
            if self._should_route_fallback():
                fb_audio, fb_ft = self._fallback_synth_bytes(text)
                if fb_audio:
                    logger.bind(tag=TAG).warning(f"兜底TTS(会话锁定)已接管: {raw_text}")
                    sent_type = (
                        SentenceType.FIRST if self.tts_audio_first_sentence
                        else SentenceType.MIDDLE
                    )
                    self.tts_audio_first_sentence = False
                    self.tts_audio_queue.put(
                        (sent_type, None, raw_text, self.current_sentence_id)
                    )
                    audio_bytes_to_data_stream(
                        fb_audio,
                        file_type=fb_ft,
                        is_opus=True,
                        callback=opus_handler,
                        sample_rate=self.conn.sample_rate,
                        opus_encoder=self.opus_encoder,
                    )
                    return None
            last_err = None
            while retries > 0:
                try:
                    METRICS.inc("fuguoai_tts_requests_total", help_text="TTS 合成请求总数")
                    # 单次合成经熔断器；开路时快速失败不再打远程 TTS。
                    audio_bytes = self._tts_breaker.call(
                        lambda: _run_on_thread_loop(self.text_to_speak(text, None))
                    )
                    if audio_bytes:
                        sent_type = (
                            SentenceType.FIRST if self.tts_audio_first_sentence
                            else SentenceType.MIDDLE
                        )
                        self.tts_audio_first_sentence = False
                        self.tts_audio_queue.put(
                            (sent_type, None, raw_text, self.current_sentence_id)
                        )
                        audio_bytes_to_data_stream(
                            audio_bytes,
                            file_type=self.audio_file_type,
                            is_opus=True,
                            callback=opus_handler,
                            sample_rate=self.conn.sample_rate,
                            opus_encoder=self.opus_encoder,
                        )
                        break
                    else:
                        # 合成成功但空音频（异常分支）：快速重试。
                        retries -= 1
                        last_err = "TTS 返回空音频"
                        if retries > 0:
                            # 短退避跨过瞬态故障窗，成功则完全不触发兜底。
                            time.sleep(0.4)
                        continue
                except CircuitOpenError:
                    last_err = "TTS 熔断开路"
                    break
                except Exception as exc:
                    # 瞬态上游错误快速重试；超时(已降级)不重试避免长阻塞。
                    last_err = f"{exc}"
                    if "超时" in str(exc):
                        break
                    retries -= 1
                    if retries > 0:
                        time.sleep(0.4)
                        continue
                    break
            if retries > 0 and last_err is None:
                self._on_primary_ok()
                logger.bind(tag=TAG).info(f"语音生成成功: {raw_text}")
                return None
            # 主 TTS 全部重试失败 → 兜底备用后端，避免整句静音。
            fb_audio, fb_ft = self._fallback_synth_bytes(text)
            if fb_audio:
                self._on_fallback_used()
                logger.bind(tag=TAG).warning(
                    f"主TTS({type(self).__name__})失败已兜底备用TTS: {raw_text}，原因: {last_err}"
                )
                sent_type = (
                    SentenceType.FIRST if self.tts_audio_first_sentence
                    else SentenceType.MIDDLE
                )
                self.tts_audio_first_sentence = False
                self.tts_audio_queue.put(
                    (sent_type, None, raw_text, self.current_sentence_id)
                )
                audio_bytes_to_data_stream(
                    fb_audio,
                    file_type=fb_ft,
                    is_opus=True,
                    callback=opus_handler,
                    sample_rate=self.conn.sample_rate,
                    opus_encoder=self.opus_encoder,
                )
                return None
            METRICS.inc("fuguoai_tts_errors_total", help_text="TTS 合成失败总数")
            logger.bind(tag=TAG).error(
                f"语音生成失败: {raw_text}，原因: {last_err or '未知'}，设备已释放 speaking"
            )
            self._release_speaking_on_failure(self.conn, self.current_sentence_id)
            return None
        else:
            # 不删文件：直接转文件流。
            if self._should_route_fallback():
                fb_tmp = self.generate_filename()
                if self._fallback_synth_file(text, fb_tmp):
                    logger.bind(tag=TAG).warning(f"兜底TTS(会话锁定)已接管(文件): {raw_text}")
                    self._on_fallback_used()
                    sent_type = (
                        SentenceType.FIRST if self.tts_audio_first_sentence
                        else SentenceType.MIDDLE
                    )
                    self.tts_audio_first_sentence = False
                    self.tts_audio_queue.put(
                        (sent_type, None, raw_text, self.current_sentence_id)
                    )
                    self._process_audio_file_stream(fb_tmp, callback=opus_handler)
                    return None
            tmp_file = self.generate_filename()
            try:
                while not os.path.exists(tmp_file) and retries > 0:
                    try:
                        _run_on_thread_loop(self.text_to_speak(text, tmp_file))
                    except Exception as exc:
                        logger.bind(tag=TAG).warning(
                            f"语音生成失败{5 - retries + 1}次: {raw_text}，错误: {exc}"
                        )
                        if os.path.exists(tmp_file):
                            os.remove(tmp_file)
                        retries -= 1
                if retries > 0:
                    self._on_primary_ok()
                    logger.bind(tag=TAG).info(
                        f"语音生成成功: {raw_text}:{tmp_file}，重试{5 - retries}次"
                    )
                else:
                    if self._fallback_synth_file(text, tmp_file):
                        self._on_fallback_used()
                        logger.bind(tag=TAG).warning(
                            f"主TTS({type(self).__name__})失败已兜底备用TTS(文件): {raw_text}"
                        )
                    else:
                        logger.bind(tag=TAG).error(
                            f"语音生成失败: {raw_text}，请检查网络或服务是否正常"
                        )
                sent_type = (
                    SentenceType.FIRST if self.tts_audio_first_sentence
                    else SentenceType.MIDDLE
                )
                self.tts_audio_first_sentence = False
                self.tts_audio_queue.put(
                    (sent_type, None, raw_text, self.current_sentence_id)
                )
                self._process_audio_file_stream(tmp_file, callback=opus_handler)
            except Exception as exc:
                logger.bind(tag=TAG).error(f"Failed to generate TTS file: {exc}")
                return None

    # ---- 主后端失败兜底：切换备用后端，避免整句静音 ----
    def _fallback_synth_bytes(self, text):
        """主 TTS 重试耗尽后兜底到备用后端，返回 (audio_bytes, file_type) 或 (None, None)。"""
        fb = getattr(self, "_fallback_tts", None)
        if fb is None:
            return (None, None)
        try:
            audio = fb._tts_breaker.call(
                lambda: _run_on_thread_loop(fb.text_to_speak(text, None))
            )
            if audio:
                return (audio, fb.audio_file_type)
        except Exception as exc:
            logger.bind(tag=TAG).error(f"兜底TTS合成异常: {exc}")
        return (None, None)

    def _fallback_synth_file(self, text, tmp_file):
        """主 TTS 重试耗尽后兜底到备用后端写入文件；返回 True 表示文件已生成。"""
        fb = getattr(self, "_fallback_tts", None)
        if fb is None:
            return False
        try:
            fb._tts_breaker.call(
                lambda: _run_on_thread_loop(fb.text_to_speak(text, tmp_file))
            )
            return os.path.exists(tmp_file)
        except Exception as exc:
            logger.bind(tag=TAG).error(f"兜底TTS文件合成异常: {exc}")
            return False

    # ---- 会话内锁定兜底状态机 ----
    def _should_route_fallback(self):
        """True 表示本次合成直接走兜底后端（跳过主后端重试）。

        - 无兜底后端 / 未锁定 → False（走主后端）
        - 已锁定 → 累计计数，每 _FB_PROBE_EVERY 句探活一次主后端（本次返回 False 走主，
          成功则解锁，失败则重新锁定）
        - 未到探活点 → True（直接兜底，音色统一）
        """
        if getattr(self, "_fallback_tts", None) is None:
            return False
        if not getattr(self, "_fallback_locked", False):
            return False
        self._fb_lock_count = getattr(self, "_fb_lock_count", 0) + 1
        if self._fb_lock_count >= getattr(self, "_FB_PROBE_EVERY", 8):
            self._fb_lock_count = 0
            return False  # 探活：本次走主后端
        return True

    def _on_primary_ok(self):
        if getattr(self, "_fallback_locked", False):
            self._fallback_locked = False
            self._fb_lock_count = 0

    def _on_fallback_used(self):
        self._fallback_locked = True
        self._fb_lock_count = 0

    def to_tts(self, text):
        raw_text = text
        text = MarkdownCleaner.clean_markdown(text)
        if self._correct_words_pattern:
            text = self._correct_words_pattern.sub(
                lambda m: self.correct_words[m.group(0)], text
            )
        retries = 5
        if self.delete_audio_file:
            # 会话锁定：直接走兜底后端，避免逐句拼音色。
            if self._should_route_fallback():
                fb_audio, fb_ft = self._fallback_synth_bytes(text)
                if fb_audio:
                    logger.bind(tag=TAG).warning(f"兜底TTS(会话锁定)已接管: {raw_text}")
                    self._on_fallback_used()
                    chunks = []
                    audio_bytes_to_data_stream(
                        fb_audio,
                        file_type=fb_ft,
                        is_opus=True,
                        callback=lambda d: chunks.append(d),
                        sample_rate=self.conn.sample_rate,
                    )
                    return chunks
            while retries > 0:
                try:
                    audio_bytes = _run_on_thread_loop(self.text_to_speak(text, None))
                    if audio_bytes:
                        self._on_primary_ok()
                        audio_datas = []
                        audio_bytes_to_data_stream(
                            audio_bytes,
                            file_type=self.audio_file_type,
                            is_opus=True,
                            callback=lambda data: audio_datas.append(data),
                            sample_rate=self.conn.sample_rate,
                        )
                        return audio_datas
                    else:
                        retries -= 1
                except Exception as exc:
                    logger.bind(tag=TAG).warning(
                        f"语音生成失败{5 - retries + 1}次: {raw_text}，错误: {exc}"
                    )
                    retries -= 1
            # 主 TTS 全部重试失败 → 兜底备用后端，返回 opus 分片。
            fb_audio, fb_ft = self._fallback_synth_bytes(text)
            if fb_audio:
                self._on_fallback_used()
                logger.bind(tag=TAG).warning(
                    f"主TTS({type(self).__name__})失败已兜底备用TTS: {raw_text}"
                )
                chunks = []
                audio_bytes_to_data_stream(
                    fb_audio,
                    file_type=fb_ft,
                    is_opus=True,
                    callback=lambda d: chunks.append(d),
                    sample_rate=self.conn.sample_rate,
                )
                return chunks
            logger.bind(tag=TAG).error(
                f"语音生成失败: {raw_text}，请检查网络或服务是否正常"
            )
            self._release_speaking_on_failure(self.conn, self.current_sentence_id)
            return None
        else:
            # 会话锁定：走兜底后端写文件。
            if self._should_route_fallback():
                fb_tmp = self.generate_filename()
                if self._fallback_synth_file(text, fb_tmp):
                    logger.bind(tag=TAG).warning(f"兜底TTS(会话锁定)已接管(文件): {raw_text}")
                    self._on_fallback_used()
                    return fb_tmp
            tmp_file = self.generate_filename()
            try:
                while not os.path.exists(tmp_file) and retries > 0:
                    try:
                        _run_on_thread_loop(self.text_to_speak(text, tmp_file))
                    except Exception as exc:
                        logger.bind(tag=TAG).warning(
                            f"语音生成失败{5 - retries + 1}次: {raw_text}，错误: {exc}"
                        )
                        if os.path.exists(tmp_file):
                            os.remove(tmp_file)
                        retries -= 1
                if retries > 0:
                    self._on_primary_ok()
                    logger.bind(tag=TAG).info(
                        f"语音生成成功: {raw_text}:{tmp_file}，重试{5 - retries}次"
                    )
                else:
                    if self._fallback_synth_file(text, tmp_file):
                        self._on_fallback_used()
                        logger.bind(tag=TAG).warning(
                            f"主TTS({type(self).__name__})失败已兜底备用TTS(文件): {raw_text}"
                        )
                    else:
                        logger.bind(tag=TAG).error(
                            f"语音生成失败: {raw_text}，请检查网络或服务是否正常"
                        )
                        self._release_speaking_on_failure(
                            self.conn, self.current_sentence_id
                        )
                return tmp_file
            except Exception as exc:
                logger.bind(tag=TAG).error(f"Failed to generate TTS file: {exc}")
                return None

    def _release_speaking_on_failure(self, conn, sentence_id=None):
        """合成失败时立即释放设备 speaking，避免卡"说话中"无声音。

        - 每个回合(sentence_id)只释放一次（_released_tts_sentences 去重）；
        - 标记本回合失败(_tts_turn_failed)，后续分段 to_tts_stream 直接跳过；
        - 释放动作经事件循环调度（本方法可能在 TTS 工作线程被调用）。
        """
        if conn is None:
            return
        if sentence_id is None:
            sentence_id = getattr(conn, "sentence_id", None)
        released = getattr(self, "_released_tts_sentences", None)
        if released is None:
            released = set()
            self._released_tts_sentences = released
        if sentence_id in released:
            return
        released.add(sentence_id)
        self._tts_turn_failed = True
        loop = getattr(conn, "loop", None)
        if loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._do_release_speaking(conn), loop)
        except Exception as exc:
            logger.bind(tag=TAG).warning(f"调度 TTS 失败释放异常: {exc}")

    async def _do_release_speaking(self, conn):
        # 仅当设备确实 speaking 才释放，避免无关场景误发 tts/stop。
        if not getattr(conn, "client_is_speaking", False):
            return
        try:
            await send_tts_message(conn, "stop", None)
        except Exception as exc:
            logger.bind(tag=TAG).warning(f"TTS 失败释放 speaking 异常: {exc}")
            try:
                conn.clearSpeakStatus()
            except Exception:
                pass

    @abstractmethod
    async def text_to_speak(self, text, output_file):
        pass

    def audio_to_pcm_data_stream(self, audio_file_path, callback: Callable[[Any], Any] = None):
        """音频文件转 PCM 编码。"""
        return audio_to_data_stream(
            audio_file_path,
            is_opus=False,
            callback=callback,
            sample_rate=self.conn.sample_rate,
            opus_encoder=None,
        )

    def audio_to_opus_data_stream(self, audio_file_path, callback: Callable[[Any], Any] = None):
        """音频文件转 Opus 编码。"""
        return audio_to_data_stream(
            audio_file_path,
            is_opus=True,
            callback=callback,
            sample_rate=self.conn.sample_rate,
            opus_encoder=self.opus_encoder,
        )

    def tts_one_sentence(
        self,
        conn,
        content_type,
        content_detail=None,
        content_file=None,
        sentence_id=None,
    ):
        """发送一句话（按句末标点切分后逐段入队）。"""
        if not sentence_id:
            if conn.sentence_id:
                sentence_id = conn.sentence_id
            else:
                sentence_id = str(uuid.uuid4().hex)
                conn.sentence_id = sentence_id
        for seg in re.split(r"([。！？!?；;\n])", content_detail):
            self.tts_text_queue.put(
                TTSMessageDTO(
                    sentence_id=sentence_id,
                    sentence_type=SentenceType.MIDDLE,
                    content_type=content_type,
                    content_detail=seg,
                    content_file=content_file,
                )
            )

    async def open_audio_channels(self, conn):
        self.conn = conn
        # 兜底后端需与主用共享 conn（其 text_to_speak 可能依赖 conn.sample_rate 等）。
        fb = getattr(self, "_fallback_tts", None)
        if fb is not None:
            fb.conn = conn
        # 按 conn.sample_rate 创建编码器；子类已创建则不覆盖。
        if not hasattr(self, "opus_encoder") or self.opus_encoder is None:
            self.opus_encoder = opus_encoder_utils.OpusEncoderUtils(
                sample_rate=conn.sample_rate, channels=1, frame_size_ms=60
            )
        self.tts_priority_thread = threading.Thread(
            target=self.tts_text_priority_thread, daemon=True
        )
        self.tts_priority_thread.start()
        self.audio_play_priority_thread = threading.Thread(
            target=self._audio_play_priority_thread, daemon=True
        )
        self.audio_play_priority_thread.start()

    def store_tts_text(self, sentence_id, text):
        """存储 sentence_id → 文本，供流式 TTS 取正确字幕；只保留最近 5 个防内存泄漏。"""
        if sentence_id and text:
            self._sentence_text_map[sentence_id] = text
            if len(self._sentence_text_map) > 5:
                oldest = next(iter(self._sentence_text_map))
                del self._sentence_text_map[oldest]

    def get_tts_text(self, sentence_id):
        """获取 sentence_id 对应文本；不存在返回 None。"""
        return self._sentence_text_map.get(sentence_id)

    def clear_tts_text(self, sentence_id):
        if sentence_id in self._sentence_text_map:
            del self._sentence_text_map[sentence_id]

    def _restore_original_text(self, text):
        if not self._reverse_words_pattern or not text:
            return text
        return self._reverse_words_pattern.sub(
            lambda m: self._reverse_words_map[m.group(0)], text
        )

    # 默认非流式；流式请在子类重写。
    def tts_text_priority_thread(self):
        while not self.conn.stop_event.is_set():
            try:
                message = self.tts_text_queue.get(timeout=1)
                if self.conn.client_abort:
                    logger.bind(tag=TAG).info("收到打断信息，终止TTS文本处理线程")
                    continue
                # 过滤旧消息：realtime 用 current_sentence_id 权威轮次比对（见 handle_opus）。
                if getattr(self.conn, "client_listen_mode", "") == "realtime":
                    if (
                        message.sentence_type != SentenceType.FIRST
                        and message.sentence_id != self.current_sentence_id
                    ):
                        continue
                else:
                    if message.sentence_id != self.conn.sentence_id:
                        continue
                if message.sentence_type == SentenceType.FIRST:
                    self.current_sentence_id = message.sentence_id
                    self.tts_stop_request = False
                    self.processed_chars = 0
                    self.tts_text_buff = []
                    self.is_first_sentence = True
                    self.tts_audio_first_sentence = True
                    # 新一轮：清除上一轮 TTS 失败兜底标记，允许本轮回退重试。
                    self._tts_turn_failed = False
                    self._released_tts_sentences = set()
                    self._tts_first_seg_mark_done = False
                elif ContentType.TEXT == message.content_type:
                    self.tts_text_buff.append(message.content_detail)
                    # 循环抽取分段：单条 MIDDLE 可能携带大量文本，需全抽避免滞留。
                    while True:
                        segment_text = self._get_segment_text()
                        if segment_text is None:
                            # 暂无可抽取片段（未成句且未达预刷新窗口）→ 退出内层循环。
                            # 必须 break 而非 continue，否则抽干后持续返回 None 会死循环。
                            break
                        if not segment_text:
                            # 抽到纯标点（游标已前移）继续抽下一段。
                            continue
                        # TTS 首段合成起点计时（首段文本交付合成）。
                        if not getattr(self, "_tts_first_seg_mark_done", False):
                            mark = getattr(self.conn, "_first_audio_mark", None)
                            if mark is not None:
                                self._tts_first_seg_mark_done = True
                                logger.bind(tag=TAG).info(
                                    f"TTS首段合成起点延迟: {time.monotonic() - mark:.3f}s"
                                )
                        self.to_tts_stream(segment_text, opus_handler=self.handle_opus)
                elif ContentType.FILE == message.content_type:
                    self._process_remaining_text_stream(opus_handler=self.handle_opus)
                    tts_file = message.content_file
                    if tts_file and os.path.exists(tts_file):
                        self._process_audio_file_stream(tts_file, callback=self.handle_opus)
                if message.sentence_type == SentenceType.LAST:
                    self._process_remaining_text_stream(opus_handler=self.handle_opus)
                    self.tts_audio_queue.put(
                        (message.sentence_type, [], message.content_detail, message.sentence_id)
                    )
            except queue.Empty:
                continue
            except Exception as exc:
                logger.bind(tag=TAG).error(
                    f"处理TTS文本失败: {str(exc)}, 类型: {type(exc).__name__}, 堆栈: {traceback.format_exc()}"
                )
                continue

    # 音频下发等待上限估算参数。
    _SEND_TIMEOUT_BASE_SEC = 5.0    # 固定余量（网络/调度抖动）
    _SEND_TIMEOUT_MAX_SEC = 300.0  # 硬上限，兜住真挂死
    _SEND_FRAME_MS = 60            # 与 sendAudioHandle.AUDIO_FRAME_DURATION 一致

    def _estimate_send_timeout(self, audio_datas, sentence_type) -> float:
        """估算 sendAudioMessage 合理等待上限（秒）。

        sendAudioMessage 按真实播放节奏限速下发，耗时与音频时长同量级：FIRST/MIDDLE
        通常很快返回；LAST 额外等整个队列播完。固定 5s 上限对任何剩余音频>5s 的正常
        回答都必然误报（实测两次精确为 5.000s/5.001s）。改为：本块时长 + 流控队列剩余
        时长 + 固定余量，夹在 [base, 300s]。估算失败回落 base。
        """
        try:
            n_frames = 1 if isinstance(audio_datas, (bytes, bytearray)) else len(audio_datas or [])
        except Exception:
            n_frames = 0
        frame_ms = self._SEND_FRAME_MS
        pending_sec = 0.0
        try:
            rc = getattr(self.conn, "audio_rate_controller", None)
            if rc is not None:
                frame_ms = getattr(rc, "frame_duration", frame_ms) or frame_ms
                pending_sec = len(rc.queue) * frame_ms / 1000.0
        except Exception:
            pending_sec = 0.0
        block_sec = n_frames * frame_ms / 1000.0
        est = block_sec + pending_sec + self._SEND_TIMEOUT_BASE_SEC
        return max(self._SEND_TIMEOUT_BASE_SEC, min(est, self._SEND_TIMEOUT_MAX_SEC))

    def _audio_play_priority_thread(self):
        enqueue_text = None
        enqueue_audio = []
        while not self.conn.stop_event.is_set():
            text = None
            try:
                try:
                    item = self.tts_audio_queue.get(timeout=0.1)
                    if len(item) == 4:
                        sentence_type, audio_datas, text, sentence_id = item
                    else:
                        sentence_type, audio_datas, text = item
                        sentence_id = None
                except queue.Empty:
                    if self.conn.stop_event.is_set():
                        break
                    continue

                if self.conn.client_abort:
                    logger.bind(tag=TAG).debug("收到打断信号，跳过当前音频数据")
                    enqueue_text, enqueue_audio = None, []
                    continue

                # 收到下一个文本开始或会话结束时上报。
                if sentence_type is not SentenceType.MIDDLE:
                    if self.report_on_last:
                        # 累积模式：FIRST 记录文本，音频持续累积，仅 LAST 统一上报。
                        if text:
                            enqueue_text = text
                        if sentence_type == SentenceType.LAST:
                            enqueue_tts_report(self.conn, enqueue_text, enqueue_audio)
                            enqueue_audio = []
                            enqueue_text = None
                    else:
                        # 非累积模式：每句分别上报。
                        if enqueue_text is not None:
                            enqueue_tts_report(self.conn, enqueue_text, enqueue_audio)
                        enqueue_audio = []
                        enqueue_text = text

                if isinstance(audio_datas, bytes):
                    enqueue_audio.append(audio_datas)

                # 发送音频：提交事件循环并带超时等待；超时上限按音频时长动态估算。
                send_timeout = self._estimate_send_timeout(audio_datas, sentence_type)
                try:
                    run_coroutine_threadsafe_with_timeout(
                        sendAudioMessage(self.conn, sentence_type, audio_datas, text, sentence_id),
                        self.conn.loop,
                        timeout=send_timeout,
                        tag="tts_play",
                    )
                except concurrent.futures.TimeoutError:
                    # 超时仅本线程不再等待，协程仍在事件循环跑完（不取消），音频未丢弃。
                    logger.bind(tag=TAG).warning(
                        f"音频下发等待超时({send_timeout:.1f}s，loop 可能真卡顿)；"
                        f"协程仍在事件循环继续执行，本线程不再等待"
                    )
                    continue

                if self.conn.max_output_size > 0 and text:
                    add_device_output(self.conn.headers.get("device-id"), len(text))
            except Exception as exc:
                # 设备主动断开时 WebSocket 已关闭，属预期内，降级 debug 并退线程避免刷 ERROR；
                # 其余真实异常仍记 ERROR。
                if self._is_connection_gone(exc) or self.conn.stop_event.is_set():
                    logger.bind(tag=TAG).debug(
                        f"audio_play_priority_thread skipped (device disconnected): {text} {exc}"
                    )
                    break
                logger.bind(tag=TAG).error(f"audio_play_priority_thread: {text} {exc}")

    @staticmethod
    def _is_connection_gone(exc: Exception) -> bool:
        """判断异常是否由设备连接断开引起（WebSocket 已关闭等预期内异常）。"""
        if _WS_CLOSED and isinstance(exc, _WS_CLOSED):
            return True
        msg = str(exc).lower()
        return any(
            k in msg
            for k in (
                "close frame",
                "connection",
                "closed",
                "websocket",
                "socket",
                "not connected",
                "broken pipe",
                "eof",
                "reset by peer",
            )
        )

    async def start_session(self, session_id):
        """会话开始钩子（默认空实现；子类按需初始化）。"""
        pass

    async def finish_session(self, session_id):
        """会话结束钩子（默认空实现；子类按需清理）。"""
        pass

    async def close(self):
        """资源清理。"""
        self._sentence_text_map.clear()
        if hasattr(self, "ws") and self.ws:
            await self.ws.close()

    def _joined_full_text(self):
        """增量拼接 tts_text_buff（O(n)）。

        旧实现每次 "".join(整个缓冲) → 长回答总字符拷贝 O(n²)。本实现只拼新增块。
        自愈：缓冲被替换/收缩时自动重建，语义与全量 join 一致。
        """
        buff = self.tts_text_buff
        if id(buff) != self._buff_id or len(buff) < self._buff_seen_count:
            self._buff_id = id(buff)
            self._joined_cache = "".join(buff)
            self._buff_seen_count = len(buff)
            self._join_work_chars += len(self._joined_cache)
        elif len(buff) > self._buff_seen_count:
            new_part = "".join(buff[self._buff_seen_count :])
            self._joined_cache += new_part
            self._buff_seen_count = len(buff)
            self._join_work_chars += len(new_part)
        return self._joined_cache

    def _get_segment_text(self):
        full_text = self._joined_full_text()
        current_text = full_text[self.processed_chars :]
        last_punct_pos = -1

        # 首句切分（杠杆 A）：开启 first_comma_boundary 时优先在第一个弱边界提前 flush；
        # 弱边界过于靠前(<min)时退回强边界。非首句/关闭时维持强边界。
        if self.is_first_sentence and getattr(self, "first_comma_boundary", True):
            first_weak = -1
            for ch in _FIRST_WEAK_BOUNDARY:
                pos = current_text.find(ch)
                if pos != -1 and (first_weak == -1 or pos < first_weak):
                    first_weak = pos
            min_seg = getattr(self, "first_comma_min_seg", 4)
            if first_weak != -1 and first_weak >= min_seg:
                last_punct_pos = first_weak
            else:
                for punct in self.first_sentence_punctuations:
                    pos = current_text.rfind(punct)
                    if (pos != -1 and last_punct_pos == -1) or (
                        pos != -1 and pos < last_punct_pos
                    ):
                        last_punct_pos = pos
        else:
            punct_set = (
                self.first_sentence_punctuations
                if self.is_first_sentence
                else self.punctuations
            )
            for punct in punct_set:
                pos = current_text.rfind(punct)
                if (pos != -1 and last_punct_pos == -1) or (
                    pos != -1 and pos < last_punct_pos
                ):
                    last_punct_pos = pos

        if last_punct_pos != -1:
            raw_seg = current_text[: last_punct_pos + 1]
            seg = textUtils.get_string_no_punctuation_or_emoji(raw_seg)
            self.processed_chars += len(raw_seg)
            # 纯标点块（如"？"）Edge TTS 无法合成，标记已处理但不单独返回（返回空串），
            # 下一轮把标点后文字一起抽；显式标点集二次判定兜底拦截全角标点误判。
            stripped = raw_seg.strip()
            if stripped and all(ch in _SYMBOL_CHARS or ch.isspace() for ch in stripped):
                return ""
            if not seg.strip():
                return ""
            if self.is_first_sentence:
                self.is_first_sentence = False
            return seg
        elif self.tts_stop_request and current_text:
            seg = current_text
            self.is_first_sentence = True
            return seg
        else:
            # V2 句内预刷新：长句/无标点长串超窗口即提前合成前一段。
            max_chars = self.tts_segment_max_chars
            if not self.tts_stop_request and len(current_text) > max_chars:
                # 窗口内从后往前找最靠后自然停顿点（绝不在任意字符硬切，避免劈开词汇）。
                window = current_text[:max_chars]
                positions = [window.rfind(ch) for ch in _REFRESH_PAUSES]
                valid = [p for p in positions if p != -1]
                if valid:
                    cut = max(valid) + 1
                else:
                    # 窗口内无自然停顿（长串英文/数字）退化为窗口末尾硬切，避免无限等待。
                    cut = max_chars
                # 停顿点过于靠前(<6字)宁可多等，避免句首逗号处切碎。
                if cut < 6:
                    return None
                seg = textUtils.get_string_no_punctuation_or_emoji(current_text[:cut])
                self.processed_chars += cut
                return seg
            return None

    def _process_audio_file_stream(self, tts_file, callback: Callable[[Any], Any]) -> None:
        """处理音频文件并转指定格式；成功合成且属本会话临时文件则清理。"""
        if tts_file.endswith(".p3"):
            p3.decode_opus_from_file_stream(tts_file, callback=callback)
        elif self.conn.audio_format == "pcm":
            self.audio_to_pcm_data_stream(tts_file, callback=callback)
        else:
            self.audio_to_opus_data_stream(tts_file, callback=callback)

        if (
            self.delete_audio_file
            and tts_file is not None
            and os.path.exists(tts_file)
            and tts_file.startswith(self.output_file)
        ):
            os.remove(tts_file)

    def _process_before_stop_play_files(self):
        for audio_datas, text in self.before_stop_play_files:
            self.tts_audio_queue.put(
                (SentenceType.MIDDLE, audio_datas, text, self.current_sentence_id)
            )
        self.before_stop_play_files.clear()
        self.tts_audio_queue.put((SentenceType.LAST, [], None, self.current_sentence_id))

    def _process_remaining_text_stream(self, opus_handler: Callable[[bytes], None] = None):
        """处理剩余文本并生成语音；返回是否成功处理。"""
        full_text = self._joined_full_text()
        remaining = full_text[self.processed_chars :]
        if remaining:
            seg = textUtils.get_string_no_punctuation_or_emoji(remaining)
            if seg:
                self.to_tts_stream(seg, opus_handler=opus_handler)
                self.processed_chars += len(full_text)
                return True
        return False

    def _apply_percentage_params(self, config):
        """按子类 TTS_PARAM_CONFIG 批量应用百分比参数。"""
        for config_key, attr_name, min_val, max_val, base_val, transform in self.TTS_PARAM_CONFIG:
            if config_key in config:
                val = convert_percentage_to_range(config[config_key], min_val, max_val, base_val)
                setattr(self, attr_name, transform(val) if transform else val)

    def _match_stream_text(self, text):
        """流式文本滑动窗口匹配，处理跨分片替换词。

        返回 (确定的文本列表, 剩余待匹配前缀)。
        """
        if not self.correct_words or not text:
            return ([text] if text else [], "")
        result = []
        pending = self._pending_prefix
        i = 0
        while i < len(text):
            char = text[i]
            test = pending + char
            matched = False
            # 遍历可能匹配的替换词（按首字分组）。
            candidates = (
                self._words_by_first_char.get(pending[0], [])
                if pending
                else self._words_by_first_char.get(char, [])
            )
            for key in candidates:
                if test == key:
                    result.append(self.correct_words[key])
                    pending = ""
                    matched = True
                    break
                elif key.startswith(test):
                    pending = test
                    matched = True
                    break
            if matched:
                i += 1
                continue
            if pending:
                result.append(pending)
                pending = ""
            if char in self._words_by_first_char:
                pending = char
            else:
                result.append(char)
            i += 1
        return result, pending

    def reset_stream_state(self):
        """重置流式处理状态，用于会话开始清理残留。"""
        self._pending_prefix = ""
