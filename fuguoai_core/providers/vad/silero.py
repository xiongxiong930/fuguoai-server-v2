"""Silero VAD provider（复果自有实现）。

基于 Silero VAD 的 ONNX 模型做语音活动检测：按 512 采样（16kHz 下 32ms）为单位
滑窗推理，双阈值判定每一帧是语音还是静音，再以 3 帧滑动窗口投票决定"当前有声音"。

复果自有演进点：

* **双阈值 + 前态延续**：高阈值以上判有声音、低阈值以下判静音、介于两者之间
  则延续上一帧结论，避免概率在阈值附近抖动造成结论翻转。
* **句末静音门限分模式**：`realtime` 全双工模式下放大门限（可由覆盖层
  `realtime_min_silence_duration_ms` 指定，默认取 2.5 倍），避免词间或思考停顿
  被切成碎片句；`auto` / `manual` 模式仍用原门限。
* **最大语音时长保护**：持续有语音超过阈值即强制触发端点，防止回声残留或滞后
  逻辑导致"有声音"粘滞、ASR 永不触发。
* **连接级状态隔离**：RNN 状态、上下文窗、语音起始时间、帧计数都挂在连接对象上，
  连接关闭时可释放。
"""

import time
import os
import numpy as np
import onnxruntime
from fuguoai_config.logger import setup_logging
from fuguoai_core.providers.vad.base import VADProviderBase

TAG = __name__
logger = setup_logging()


class VADProvider(VADProviderBase):
    def __init__(self, config):
        logger.bind(tag=TAG).info("SileroVAD", config)

        model_path = os.path.join(
            config["model_dir"], "src", "silero_vad", "data", "silero_vad.onnx"
        )
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self.session = onnxruntime.InferenceSession(
            model_path, providers=["CPUExecutionProvider"], sess_options=opts
        )

        threshold = config.get("threshold", "0.5")
        threshold_low = config.get("threshold_low", "0.2")
        min_silence_duration_ms = config.get("min_silence_duration_ms", "1000")
        max_speech_duration_ms = config.get("max_speech_duration_ms", "15000")

        # 注意 falsy 回退：配置为空串/0/None 一律回落到默认值
        self.vad_threshold = float(threshold) if threshold else 0.5
        self.vad_threshold_low = float(threshold_low) if threshold_low else 0.2

        self.silence_threshold_ms = (
            int(min_silence_duration_ms) if min_silence_duration_ms else 1000
        )
        self.max_speech_duration_ms = (
            int(max_speech_duration_ms) if max_speech_duration_ms else 15000
        )
        # realtime 全双工专属：放大"句末静音"判定门限（避免词间/思考停顿被切句）。
        # 覆盖层可配 realtime_min_silence_duration_ms；不配则默认 = silence_threshold_ms * 2.5。
        # auto/manual 模式仍用原 silence_threshold_ms，不受影响。
        rt_silence = config.get("realtime_min_silence_duration_ms")
        self.realtime_silence_threshold_ms = int(rt_silence) if rt_silence else None

        self.frame_window_threshold = 3
        # 诊断日志：每N帧输出一次 VAD 内部状态
        self._vad_diag_interval = 100

    def _init_connection_state(self, conn):
        """为连接初始化独立的 VAD 状态（已存在则保留原值，不覆盖）"""
        if not hasattr(conn, "_vad_state"):
            conn._vad_state = np.zeros((2, 1, 128), dtype=np.float32)
        if not hasattr(conn, "_vad_context"):
            conn._vad_context = np.zeros((1, 64), dtype=np.float32)
        if not hasattr(conn, "_vad_speech_start_time"):
            conn._vad_speech_start_time = 0.0
        if not hasattr(conn, "_vad_frame_counter"):
            conn._vad_frame_counter = 0

    def release_conn_resources(self, conn):
        """释放连接的 VAD 资源（连接关闭时调用）"""
        for attr in ("_vad_state", "_vad_context", "_vad_speech_start_time", "_vad_frame_counter"):
            if hasattr(conn, attr):
                try:
                    delattr(conn, attr)
                except Exception as e:
                    logger.bind(tag=TAG).error(f"VAD 资源释放异常: {e}")

    def is_vad(self, conn, pcm_frame):
        # 手动模式：直接返回True，不进行实时VAD检测，所有音频都缓存
        if conn.client_listen_mode == "manual":
            return True

        try:
            self._init_connection_state(conn)
            conn._vad_frame_counter += 1

            # pcm_frame已经是处理后的PCM数据
            conn.client_audio_buffer.extend(pcm_frame)

            client_have_voice = False
            max_speech_prob = 0.0
            while len(conn.client_audio_buffer) >= 512 * 2:
                chunk = conn.client_audio_buffer[: 512 * 2]
                conn.client_audio_buffer = conn.client_audio_buffer[512 * 2 :]

                audio_int16 = np.frombuffer(chunk, dtype=np.int16)
                audio_float32 = audio_int16.astype(np.float32) / 32768.0
                audio_input = np.concatenate(
                    [conn._vad_context, audio_float32.reshape(1, -1)], axis=1
                ).astype(np.float32)

                ort_inputs = {
                    "input": audio_input,
                    "state": conn._vad_state,
                    "sr": np.array(16000, dtype=np.int64),
                }
                out, state = self.session.run(None, ort_inputs)

                conn._vad_state = state
                conn._vad_context = audio_input[:, -64:]
                speech_prob = out.item()
                max_speech_prob = max(max_speech_prob, speech_prob)

                # 双阈值判断：高于高阈判有声音，低于低阈判静音，介于两者之间延续前态
                if speech_prob >= self.vad_threshold:
                    is_voice = True
                elif speech_prob <= self.vad_threshold_low:
                    is_voice = False
                else:
                    is_voice = conn.last_is_voice

                # 声音没低于最低值则延续前一个状态，判断为有声音
                conn.last_is_voice = is_voice

                # 更新滑动窗口
                conn.client_voice_window.append(is_voice)
                client_have_voice = (
                    conn.client_voice_window.count(True) >= self.frame_window_threshold
                )

                # realtime 全双工：放大句末静音门限，避免词间/思考停顿被切成碎片句
                if conn.client_listen_mode == "realtime":
                    _silence_ms = (
                        self.realtime_silence_threshold_ms
                        if self.realtime_silence_threshold_ms is not None
                        else max(self.silence_threshold_ms, int(self.silence_threshold_ms * 2.5))
                    )
                else:
                    _silence_ms = self.silence_threshold_ms
                # 如果之前有声音，但本次没有声音，且与上次有声音的时间差已经超过了静默阈值，则认为已经说完一句话
                if conn.client_have_voice and not client_have_voice:
                    stop_duration = time.time() * 1000 - conn.vad_last_voice_time
                    if stop_duration >= _silence_ms:
                        conn.client_voice_stop = True
                if client_have_voice:
                    conn.client_have_voice = True
                    conn.vad_last_voice_time = time.time() * 1000

                # 最大语音时长保护：若持续有语音超过阈值，强制触发端点
                # 防止回声残留/滞后逻辑导致 client_have_voice 粘滞，永不触发 ASR
                if conn.client_have_voice and conn._vad_speech_start_time == 0.0:
                    conn._vad_speech_start_time = time.time() * 1000
                if not conn.client_have_voice:
                    conn._vad_speech_start_time = 0.0
                if (
                    conn.client_have_voice
                    and conn._vad_speech_start_time > 0.0
                    and (time.time() * 1000 - conn._vad_speech_start_time) >= self.max_speech_duration_ms
                ):
                    logger.bind(tag=TAG).warning(
                        f"[VAD超时保护] 持续语音超过 {self.max_speech_duration_ms}ms，"
                        f"强制触发端点检测 (speech_prob_max={max_speech_prob:.3f})"
                    )
                    conn.client_voice_stop = True
                    conn._vad_speech_start_time = 0.0

            # 诊断日志：每N帧或状态变化时输出
            state_changed = (
                getattr(conn, "_vad_last_logged_voice", None) != conn.client_have_voice
            )
            if (
                state_changed
                or conn._vad_frame_counter % self._vad_diag_interval == 0
            ):
                conn._vad_last_logged_voice = conn.client_have_voice
                logger.bind(tag=TAG).debug(
                    f"[VAD诊断] frame={conn._vad_frame_counter} "
                    f"have_voice={conn.client_have_voice} "
                    f"local_have_voice={client_have_voice} "
                    f"speech_prob_max={max_speech_prob:.3f} "
                    f"stop={conn.client_voice_stop} "
                    f"speaking={conn.client_is_speaking}"
                )

            return client_have_voice
        except Exception as e:
            # 异常路径不返回 False，而是隐式返回 None（既有契约，调用方按 falsy 处理）
            logger.bind(tag=TAG).error(f"Error processing audio packet: {e}")
