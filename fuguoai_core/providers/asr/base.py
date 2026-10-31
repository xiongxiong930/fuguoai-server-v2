"""语音识别（ASR）提供方基类 —— 复果 AI 自有实现（clean-room 重写）。

主链路固定为「收音 → 识别 → 上报 → 应答」，各识别后端（腾讯云 / 本地 FunASR /
云端流式等）只需实现 speech_to_text。本实现完全独立编写，仅在公开契约（类名、
被 connection 调用的方法名、抽象方法签名）上与调用方一致；WAV 编码采用手写 RIFF
头（不走标准库 wave 模块），文件落盘 / 磁盘探测 / 识别编排均按复果自有结构组织。

复果自有演进（非上游逻辑，门逐条验证）：
- 收音线程以超时方式把每帧提交回事件循环，避免同步热点（VAD/AEC 推理）卡死 loop
  引发全链路线程雪崩；
- 识别与声纹识别并发等待，任一失败互不影响；
- 空识别（用户沉默 / VAD 误触发）视为合法结果并记成功，否则会把熔断器打 open
  永久禁用识别；失败时合成兜底语音并主动让固件停止续听，避免重连风暴。
"""

import os
import uuid
import json
import time
import queue
import asyncio
import tempfile
import traceback
import threading
import concurrent.futures
import struct

from abc import ABC, abstractmethod
from typing import Optional, Tuple, List, NamedTuple, TYPE_CHECKING

from fuguoai_config.logger import setup_logging
from fuguoai_core.providers.asr.dto.dto import InterfaceType
from fuguoai_core.handle.receiveAudioHandle import startToChat, handleAudioMessage
from fuguoai_core.handle.reportHandle import enqueue_asr_report
from fuguoai_core.utils.util import remove_punctuation_and_length
from fuguoai_core.handle.sendAudioHandle import send_stt_message, SentenceType
from fuguoai_core.utils.circuit_breaker import get_breaker
from fuguoai_core.utils.asyncio_util import run_coroutine_threadsafe_with_timeout

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

# 收音线程把音频帧提交回事件循环时的最大等待秒数；超时即跳过本帧防雪崩。
_SUBMIT_TIMEOUT = 5
# 自动模式触发识别所需最小音频字节数（每帧 1920 字节，约 15 帧）。
_MIN_BYTES = 1920 * 15
# WAV 写出参数：单声道 / 16bit / 16kHz。
_WAV_CHANNELS = 1
_WAV_SAMPLE_WIDTH = 2
_WAV_FRAME_RATE = 16000
# 识别失败 / 空结果时的兜底播报。
_FALLBACK_PHRASE = "不好意思，我没听清，能再说一遍吗？"


class ASRProviderBase(ABC):
    """语音识别提供方基类：子类须实现 speech_to_text。"""

    def __init__(self):
        # 最近一次识别工件；默认无，供 get_current_artifacts 安全返回。
        self._current_artifacts = None

    # ---- 音频通道与收音线程 ----

    async def open_audio_channels(self, conn: "ConnectionHandler"):
        """启动守护收音线程，随连接结束自动回收。"""
        conn.asr_priority_thread = threading.Thread(
            target=self._drain_audio_queue, args=(conn,), daemon=True
        )
        conn.asr_priority_thread.start()

    def _drain_audio_queue(self, conn: "ConnectionHandler"):
        """收音线程：按到达顺序消费音频帧并提交回事件循环处理。"""
        while not conn.stop_event.is_set():
            try:
                item = conn.asr_audio_queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                # 带超时提交：loop 因同步 CPU 热点卡顿时不会无限挂起本线程。
                run_coroutine_threadsafe_with_timeout(
                    handleAudioMessage(conn, item),
                    conn.loop,
                    timeout=_SUBMIT_TIMEOUT,
                    tag="asr",
                )
            except concurrent.futures.TimeoutError:
                logger.bind(tag=TAG).warning(
                    "音频处理提交到事件循环超时(loop可能卡顿)，跳过本帧防雪崩"
                )
                continue
            except Exception as exc:
                logger.bind(tag=TAG).error(
                    f"处理ASR文本失败: {exc}, 类型: {type(exc).__name__}, 堆栈: {traceback.format_exc()}"
                )
                continue

    # ---- 接收与触发识别 ----

    async def receive_audio(self, conn: "ConnectionHandler", pcm_frame, audio_have_voice):
        """接收一帧音频，并在自动模式下判断是否触发识别。"""
        conn.asr_audio.append(pcm_frame)
        if conn.client_listen_mode == "manual":
            return
        # 自动/实时模式：仅当本帧与历史均无语音时裁剪缓存，避免无限增长。
        if not audio_have_voice and not conn.client_have_voice:
            conn.asr_audio = conn.asr_audio[-10:]
            return
        if conn.asr.interface_type != InterfaceType.STREAM and conn.client_voice_stop:
            blob = b"".join(conn.asr_audio)
            if len(blob) > _MIN_BYTES:
                await self.handle_voice_stop(conn, [blob])
            conn.reset_audio_states()

    async def handle_voice_stop(self, conn: "ConnectionHandler", asr_audio_task: List[bytes]):
        """并行处理 ASR 与声纹识别，并驱动上报 / 应答 / 降级。"""
        try:
            t0 = time.monotonic()

            transcript, who = await self._dispatch_recognition(conn, asr_audio_task)
            packed, length_src = self._serialize(transcript, who)

            cost = time.monotonic() - t0
            logger.bind(tag=TAG).info(f"ASR总处理耗时: {cost:.3f}s")

            chars, _ = remove_punctuation_and_length(length_src)
            self.stop_ws_connection()

            if chars > 0:
                # 空识别是合法结果而非故障：正常"没说话"也记成功，避免熔断被打 open。
                get_breaker("asr").record_success()
                snap = asr_audio_task.copy()
                enqueue_asr_report(conn, packed, snap)
                await startToChat(conn, packed)
            else:
                get_breaker("asr").record_success()
                await self._recover_with_prompt(conn)
        except Exception as exc:
            logger.bind(tag=TAG).error(f"处理语音停止失败: {exc}")
            logger.bind(tag=TAG).debug(f"异常详情: {traceback.format_exc()}")

    async def _dispatch_recognition(
        self, conn: "ConnectionHandler", chunks: List[bytes]
    ) -> Tuple[object, str]:
        """并发跑 ASR 与声纹识别，返回 (识别结果, 说话人)。"""
        merged = b"".join(chunks)
        recog = self.speech_to_text_wrapper(chunks, conn.session_id)
        if conn.voiceprint_provider and merged:
            wav = self._encode_wav(merged)
            spoken = conn.voiceprint_provider.identify_speaker(wav, conn.session_id)
            recognized, spoken = await asyncio.gather(
                recog, spoken, return_exceptions=True
            )
        else:
            recognized = await recog
            spoken = None

        if isinstance(recognized, Exception):
            logger.bind(tag=TAG).error(f"ASR识别失败: {recognized}")
            transcript: object = ""
        else:
            transcript, _ = recognized

        if isinstance(spoken, Exception):
            logger.bind(tag=TAG).error(f"声纹识别失败: {spoken}")
            who = ""
        else:
            who = spoken if spoken else ""
        return transcript, who

    def _serialize(self, transcript: object, who: str) -> Tuple[str, str]:
        """把识别结果（dict 或纯文本）与说话人序列化为下发串与长度校验源。"""
        if isinstance(transcript, dict):
            if who:
                transcript["speaker"] = who
            if transcript.get("language"):
                logger.bind(tag=TAG).info(f"识别语言: {transcript['language']}")
            if transcript.get("emotion"):
                logger.bind(tag=TAG).info(f"识别情绪: {transcript['emotion']}")
            if transcript.get("content"):
                logger.bind(tag=TAG).info(f"识别文本: {transcript['content']}")
            if who:
                logger.bind(tag=TAG).info(f"识别说话人: {who}")
            return json.dumps(transcript, ensure_ascii=False), transcript.get("content", "")
        if transcript:
            logger.bind(tag=TAG).info(f"识别文本: {transcript}")
        if who:
            logger.bind(tag=TAG).info(f"识别说话人: {who}")
        return self._attach_speaker(str(transcript), who), str(transcript)

    async def _recover_with_prompt(self, conn: "ConnectionHandler"):
        """识别失败 / 空结果时的降级：合成兜底语音并让设备停止续听。"""
        try:
            await send_stt_message(conn, _FALLBACK_PHRASE)
            synth = getattr(conn, "tts", None)
            if synth is not None and hasattr(synth, "to_tts"):
                loop = conn.loop
                ex = getattr(conn, "executor", None)
                opus = await loop.run_in_executor(ex, synth.to_tts, _FALLBACK_PHRASE)
                conn.tts.tts_audio_queue.put((SentenceType.LAST, opus, _FALLBACK_PHRASE))
                try:
                    await conn.safe_send(
                        json.dumps(
                            {
                                "type": "listen",
                                "state": "stop",
                                "session_id": conn.session_id,
                            }
                        )
                    )
                except Exception:
                    pass
            else:
                logger.bind(tag=TAG).warning("ASR 降级提示：TTS 不可用，仅记录不播报")
        except Exception as exc:
            logger.bind(tag=TAG).warning(f"ASR 降级提示发送失败: {exc}")
            try:
                conn.tts.tts_audio_queue.put((SentenceType.LAST, [], None))
            except Exception:
                pass

    # ---- 文本 / 音频辅助 ----

    def _attach_speaker(self, text: str, who: str) -> str:
        """把说话人信息并入纯文本 ASR 结果。"""
        if who and who.strip():
            return json.dumps(
                {"speaker": who, "content": text}, ensure_ascii=False
            )
        return text

    def _encode_wav(self, pcm: bytes) -> bytes:
        """手写 RIFF 头，把裸 PCM 字节封装为 16kHz/16bit/单声道 WAV。"""
        if len(pcm) == 0:
            logger.bind(tag=TAG).warning("PCM数据为空，无法转换WAV")
            return b""
        if len(pcm) % 2 != 0:
            pcm = pcm[:-1]
        n = len(pcm)
        header = (
            b"RIFF"
            + struct.pack("<I", 36 + n)
            + b"WAVE"
            + b"fmt "
            + struct.pack(
                "<IHHIIHH",
                16,
                1,
                _WAV_CHANNELS,
                _WAV_FRAME_RATE,
                _WAV_FRAME_RATE * _WAV_CHANNELS * _WAV_SAMPLE_WIDTH,
                _WAV_CHANNELS * _WAV_SAMPLE_WIDTH,
                _WAV_SAMPLE_WIDTH * 8,
            )
            + b"data"
            + struct.pack("<I", n)
        )
        return header + pcm

    def stop_ws_connection(self):
        """停止 WebSocket 连接（子类按需覆写）。"""
        pass

    async def close(self):
        """释放资源（子类按需覆写）。"""
        pass

    # ---- 工件与文件管理 ----

    class RecognitionArtifacts(NamedTuple):
        """一次识别所用的音频工件集合。"""

        pcm_frames: List[bytes]
        """PCM音频帧列表"""
        pcm_bytes: bytes
        """合并后的PCM音频字节数据"""
        file_path: Optional[str]
        """WAV文件路径"""
        temp_path: Optional[str]
        """临时WAV文件路径"""

    def get_current_artifacts(self) -> Optional["ASRProviderBase.RecognitionArtifacts"]:
        """返回最近一次识别所用的音频工件。"""
        return self._current_artifacts

    def requires_file(self) -> bool:
        """是否需要文件输入。"""
        return False

    def prefers_temp_file(self) -> bool:
        """是否优先使用临时文件。"""
        return False

    def _stash_wav(self, pcm: bytes, session_id: str) -> Tuple[Optional[str], Optional[str]]:
        """按 requires_file / prefers_temp_file 约定落盘，返回 (正式路径, 临时路径)。"""
        fpath = None
        tpath = None
        blob = self._encode_wav(pcm)
        if self.requires_file() and self.prefers_temp_file():
            fd, tpath = tempfile.mkstemp(suffix=".wav")
            with os.fdopen(fd, "wb") as fh:
                fh.write(blob)
        if (hasattr(self, "delete_audio_file") and not self.delete_audio_file) or (
            self.requires_file() and not self.prefers_temp_file()
        ):
            fname = f"asr_{session_id}_{uuid.uuid4().hex}.wav"
            fpath = os.path.join(self.output_dir, fname)
            with open(fpath, "wb") as fh:
                fh.write(blob)
        return fpath, tpath

    async def speech_to_text_wrapper(
        self, pcm_data: List[bytes], session_id: str
    ) -> Tuple[Optional[str], Optional[str]]:
        """带熔断与文件管理的识别包装：返回 (识别文本, 文件路径)。"""
        guard = get_breaker("asr")
        # 熔断开路时快速失败，不再对故障远程服务反复打请求。
        if not guard.allow():
            logger.bind(tag=TAG).warning("ASR 熔断开路，跳过远程识别（快速失败）")
            return None, None
        try:
            merged = b"".join(pcm_data)
            fs = os.statvfs(self.output_dir)
            if fs.f_bavail * fs.f_frsize < len(merged) * 2:
                raise OSError("磁盘空间不足")
            fpath, tpath = self._stash_wav(merged, session_id)
            artifact = (
                None
                if not merged
                else ASRProviderBase.RecognitionArtifacts(
                    pcm_data, merged, fpath, tpath
                )
            )
            self._current_artifacts = artifact
            text, _ = await self.speech_to_text(pcm_data, session_id, artifact)
            return text, fpath
        except OSError as exc:
            logger.bind(tag=TAG).error(f"文件操作错误: {exc}")
            return None, None
        except Exception as exc:
            logger.bind(tag=TAG).error(f"语音识别失败: {exc}")
            return None, None
        finally:
            try:
                if tpath and os.path.exists(tpath):
                    os.unlink(tpath)
                if (
                    hasattr(self, "delete_audio_file")
                    and self.delete_audio_file
                    and fpath
                    and os.path.exists(fpath)
                ):
                    os.remove(fpath)
            except Exception as exc:
                logger.bind(tag=TAG).error(f"文件清理失败: {exc}")

    @abstractmethod
    async def speech_to_text(
        self,
        opus_data: List[bytes],
        session_id: str,
        artifacts: Optional[RecognitionArtifacts] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """将语音数据转换为文本；子类实现。"""
        pass
