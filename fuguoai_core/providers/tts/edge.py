import os
import time
import uuid
import asyncio
import threading
import edge_tts
from datetime import datetime
from fuguoai_config.logger import setup_logging
from fuguoai_core.providers.tts.base import TTSProviderBase

# 流式边到边解码依赖：PyAV 渐进 demux/decode MP3(边收边解)，numpy 做 PCM 整形。
# 无 av/numpy 的环境(如某些本地回归 venv)降级为整段合成(text_to_stream 抛错，
# 由 base.py 回落 text_to_speak)，不劣于现状。159 容器已装 av/numpy。
try:
    import av as _AV
except Exception:  # pragma: no cover - 无 av 环境走整段合成
    _AV = None
try:
    import numpy as _NP
except Exception:  # pragma: no cover
    _NP = None

TAG = __name__
logger = setup_logging()

# TTS 并发闸：限制同时打到微软 Edge TTS 外部服务的请求数。
# 多设备同时开口 = 多路并行外部请求，上游限流/抖动时全部互相等待放大卡顿
# （C3 卡顿同源）。Semaphore 超额排队而非全部打爆上游；单路仍受 P0-1
# wait_for(timeout) 保护，不会无限占用槽位。
_TTS_SEM_LIMIT = int(os.environ.get("FUGUOAI_TTS_MAX_CONCURRENT", "20"))


def _get_tts_semaphore():
    """惰性获取并发闸；首次调用时绑定当前事件循环（Python3.10 下安全）。"""
    global _tts_semaphore
    if _tts_semaphore is None:
        _tts_semaphore = asyncio.Semaphore(_TTS_SEM_LIMIT)
    return _tts_semaphore


_tts_semaphore = None


# ==========================================================================
# EdgeTTS 连接保活（消除 idle 后首句 ~12s 冷启动尖峰）
# --------------------------------------------------------------------------
# 根因(2026-07-22 实测): base.py 每次合成用 asyncio.run() 新建/销毁临时事件
# 循环，aiohttp 连接池随 loop 销毁无法跨合成复用；真正的"热态"落在 OS/进程级
# (DNS 缓存 / TCP conntrack / TLS session ticket)。对话停顿 >~30-40s 后这些
# 路径状态失活，下一句要重建连接、SYN 重传累加，首帧音频延迟飙到 ~12s。
#
# 方案: 进程级单例后台线程(独立常驻 loop)，idle 期周期性做一次极短静默合成，
# 维持网络路径热态。预热音频**直接丢弃、绝不下发任何设备**；带超时保护、异常
# 静默；活跃对话时(距上次真实合成不足一个周期)自动跳过以省上游请求。
# 开关: FUGUOAI_TTS_KEEPALIVE(默认 on) / _INTERVAL(15s) / _TIMEOUT(10s)。
# ==========================================================================
_KEEPALIVE_ENABLED = os.environ.get("FUGUOAI_TTS_KEEPALIVE", "true").lower() in (
    "1", "true", "yes", "on",
)
_KEEPALIVE_INTERVAL = int(os.environ.get("FUGUOAI_TTS_KEEPALIVE_INTERVAL", "15"))
_KEEPALIVE_TIMEOUT = int(os.environ.get("FUGUOAI_TTS_KEEPALIVE_TIMEOUT", "10"))
_last_synth_ts = 0.0  # 最近一次真实合成的时刻(text_to_speak 更新)
_keepalive_started = False
_keepalive_provider_ref = None  # 预热用的参数模板(最近实例化的 provider)
_keepalive_lock = threading.Lock()


async def _keepalive_warm_once(voice, rate, volume, pitch, timeout):
    """做一次极短静默合成，仅用于打通/维持到微软 speech 服务的连接路径。音频丢弃。"""
    communicate = edge_tts.Communicate(
        "嗯", voice=voice, rate=rate, volume=volume, pitch=pitch,
    )
    try:
        async def _drain():
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    pass  # 预热音频直接丢弃，绝不下发设备
        await asyncio.wait_for(_drain(), timeout=timeout)
    finally:
        try:
            await communicate.close()
        except Exception:
            pass


def _keepalive_loop():
    """进程级单例后台线程：常驻独立事件循环，idle 期周期性预热。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    logger.bind(tag=TAG).info(
        f"EdgeTTS 连接保活线程已启动 (周期 {_KEEPALIVE_INTERVAL}s, 预热维持网络路径热态)"
    )
    while True:
        try:
            time.sleep(_KEEPALIVE_INTERVAL)
            prov = _keepalive_provider_ref
            if prov is None:
                continue  # 尚无任何连接实例，无参数模板，跳过
            # 距上次真实合成不足一个周期 => 用户在密集对话，路径本就热，跳过省流量
            if (time.monotonic() - _last_synth_ts) < _KEEPALIVE_INTERVAL:
                continue
            loop.run_until_complete(
                _keepalive_warm_once(
                    prov.voice, prov.edge_rate, prov.edge_volume,
                    prov.edge_pitch, _KEEPALIVE_TIMEOUT,
                )
            )
            logger.bind(tag=TAG).debug("EdgeTTS 保活预热成功")
        except Exception as e:
            logger.bind(tag=TAG).debug(f"EdgeTTS 保活预热失败(忽略): {e}")


def _ensure_keepalive(provider):
    """更新预热参数模板；进程内首次调用时启动保活线程(单例)。"""
    global _keepalive_started, _keepalive_provider_ref
    _keepalive_provider_ref = provider
    if not getattr(provider, "_keepalive_enabled", _KEEPALIVE_ENABLED) or _keepalive_started:
        return
    with _keepalive_lock:
        if _keepalive_started:
            return
        _keepalive_started = True
        threading.Thread(
            target=_keepalive_loop, name="edgetts-keepalive", daemon=True,
        ).start()


# ==========================================================================
# 流式边到边：渐进式 MP3 缓冲(生产者写 / 消费者 av.open 读解码)
# --------------------------------------------------------------------------
# EdgeTTS 的 communicate.stream() 逐块返回 MP3(网络流式)，但整段合成要等所有
# MP3 收齐才能解——这正是 ~3s 首包地板的根因。这里用线程安全的 file-like 缓冲
# 桥接：生产者线程把 MP3 块 write 进缓冲，消费线程用 av.open(buffer) 渐进
# demux+decode 成 16-bit PCM 即时喂回调，实现边收边解边下发。
#
# read() 阻塞到有数据或 EOF；仅在「长时间无新数据且未 EOF」时抛错(防解码死锁)，
# 绝不返回空字节——空字节会被 PyAV 误判为 EOF 而截断音频。
# ==========================================================================
class _StreamBuffer:
    def __init__(self, read_timeout=30.0):
        self._buf = b""
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._eof = False
        self._reads = 0
        self._read_timeout = read_timeout

    def write(self, data: bytes):
        if not data:
            return
        with self._cond:
            self._buf += data
            self._cond.notify_all()

    def done(self):
        """生产者结束(正常或异常 finally)时调用，标记流终。"""
        with self._cond:
            self._eof = True
            self._cond.notify_all()

    def read(self, n=-1):
        with self._cond:
            while not self._buf and not self._eof:
                self._cond.wait(timeout=self._read_timeout)
                if not self._buf and not self._eof:
                    # 生产者异常长时无数据：抛错(而非返回空)防 PyAV 误判 EOF
                    raise IOError("EdgeTTS 流读取超时(无新数据且未结束)，防解码死锁")
            if not self._buf:
                return b""  # 真正 EOF
            if n == -1 or n > len(self._buf):
                data = self._buf
                self._buf = b""
            else:
                data = self._buf[:n]
                self._buf = self._buf[n:]
            self._reads += 1
            return data

    def seekable(self):
        return False

    def close(self):
        pass


class TTSProvider(TTSProviderBase):
    TTS_PARAM_CONFIG = [
        ("ttsVolume", "volume", 0, 100, 50, int),
        ("ttsRate", "speech_rate", -100, 100, 0, int),
        ("ttsPitch", "pitch_rate", -100, 100, 0, int),
    ]

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        if config.get("private_voice"):
            self.voice = config.get("private_voice")
        else:
            self.voice = config.get("voice")
        self.audio_file_type = config.get("format", "mp3")

        volume = config.get("volume", "50")
        self.volume = int(volume) if volume else 50

        speech_rate = config.get("rate", "0")
        self.speech_rate = int(speech_rate) if speech_rate else 0

        pitch_rate = config.get("pitch", "0")
        self.pitch_rate = int(pitch_rate) if pitch_rate else 0

        # 应用百分比调整
        self._apply_percentage_params(config)

        self.edge_rate = f"{self.speech_rate:+}%"
        self.edge_volume = f"{self.volume:+}%"
        self.edge_pitch = f"{self.pitch_rate:+}Hz"

        # 启动/刷新 EdgeTTS 连接保活(进程级单例，消除 idle 后首句冷启动尖峰)
        # keepalive 可被本 provider 配置项覆盖(默认跟随环境变量)。关闭后避免保活
        # 预热请求徒增微软 Edge TTS 限频配额、拖垮真实对话(2026-08-01 卡顿根因)
        ka = config.get("keepalive")
        if ka is not None:
            self._keepalive_enabled = str(ka).lower() in ("1", "true", "yes", "on")
        else:
            self._keepalive_enabled = _KEEPALIVE_ENABLED
        _ensure_keepalive(self)

    def generate_filename(self, extension=".mp3"):
        return os.path.join(
            self.output_file,
            f"tts-{datetime.now().date()}@{uuid.uuid4().hex}{extension}",
        )

    async def text_to_speak(self, text, output_file):
        # 记录真实合成时刻：保活线程据此在密集对话时跳过预热(路径本就热)
        global _last_synth_ts
        _last_synth_ts = time.monotonic()
        communicate = edge_tts.Communicate(
            text,
            voice=self.voice,
            rate=self.edge_rate,
            volume=self.edge_volume,
            pitch=self.edge_pitch,
        )
        # P0-1: 硬性超时，防止微软 speech 服务卡死导致 TTS 线程无限挂起。
        # 2026-07-12 暴露：edge_tts 默认无 HTTP 超时，国内访问偶发限流时
        # 表现为"连上不返回、服务器零报错"。超时被 base.py 捕获后重试/失败，
        # 快速释放线程，设备得以尽快重连重试，而非卡死数分钟。
        timeout = getattr(self, "tts_timeout", 15)
        try:
            if output_file:
                # 确保目录存在并创建空文件
                os.makedirs(os.path.dirname(output_file), exist_ok=True)
                with open(output_file, "wb") as f:
                    pass

                # 流式写入音频数据
                async def _save():
                    with open(output_file, "ab") as f:  # 追加模式避免覆盖
                        async for chunk in communicate.stream():
                            if chunk["type"] == "audio":  # 只处理音频数据块
                                f.write(chunk["data"])

                async with _get_tts_semaphore():
                    await asyncio.wait_for(_save(), timeout=timeout)
                return None
            else:
                # 返回音频二进制数据
                async def _collect():
                    buf = b""
                    async for chunk in communicate.stream():
                        if chunk["type"] == "audio":
                            buf += chunk["data"]
                    return buf

                async with _get_tts_semaphore():
                    return await asyncio.wait_for(_collect(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                await communicate.close()
            except Exception:
                pass
            raise Exception(f"Edge TTS 请求超时(>{timeout}s)，已中止以免线程挂死")
        except Exception as e:
            try:
                await communicate.close()
            except Exception:
                pass
            raise Exception(f"Edge TTS请求失败: {e}")

    async def text_to_stream(self, text, pcm_callback):
        """EdgeTTS 流式边到边：增量收 MP3 → PyAV 渐进解 16-bit PCM → 即时喂
        pcm_callback，由上层 encode_pcm_to_opus_stream 边编码边下发，削掉整段
        合成(~3s)首包地板。

        仅 24k 设备走流式：Edge 原生输出即 24k/单声道/16bit，与设备普通连接采样
        率一致免重采样；非 24k(如对讲 16k) 抛 ValueError，交由 base.py 回落
        整段合成(text_to_speak 经 pydub 重采样，保正确)。av/numpy 不可用时抛
        RuntimeError 同样回落整段合成(不劣于现状)。

        生产者(协程拉 MP3 块)与消费者(线程内 av 渐进解码)并发，任一方异常即时
        取消/收尾对方，避免互相等待死锁。
        """
        if not text or not text.strip():
            return None
        conn = getattr(self, "conn", None)
        conn_sr = getattr(conn, "sample_rate", 24000) if conn is not None else 24000
        if conn_sr != 24000:
            raise ValueError(
                f"EdgeTTS 流式仅支持 24k 采样率(conn.sample_rate={conn_sr})，回落整段合成"
            )
        if _AV is None or _NP is None:
            raise RuntimeError("EdgeTTS 流式依赖 av/numpy 不可用，回落整段合成")
        # 记录真实合成时刻：保活线程据此在密集对话时跳过预热(路径本就热)
        global _last_synth_ts
        _last_synth_ts = time.monotonic()

        communicate = edge_tts.Communicate(
            text, voice=self.voice, rate=self.edge_rate,
            volume=self.edge_volume, pitch=self.edge_pitch,
        )
        sb = _StreamBuffer()

        async def _produce():
            try:
                async with _get_tts_semaphore():
                    async for chunk in communicate.stream():
                        if chunk.get("type") == "audio" and chunk.get("data"):
                            sb.write(chunk["data"])
            finally:
                sb.done()

        def _consume():
            # 阻塞解码跑在线程池；边收边解，逐帧 PCM 喂回调
            with _AV.open(sb, format="mp3") as container:
                inlet = container.streams.audio[0]
                for frame in container.decode(inlet):
                    arr = frame.to_ndarray()
                    # PyAV 解 MP3 给出 float32([-1,1])；直接 .astype(int16) 会把
                    # 0.65 等截断为 0 → 全零静音(b36d8d9 静音根因)。浮点须缩放。
                    if _NP.issubdtype(arr.dtype, _NP.floating):
                        pcm16 = _NP.clip(arr, -1.0, 1.0)
                        pcm16 = (pcm16 * 32767.0).astype(_NP.int16)
                    else:
                        pcm16 = _NP.ascontiguousarray(arr).astype(_NP.int16)
                    pcm_callback(pcm16.tobytes())

        loop = asyncio.get_event_loop()
        produce_task = asyncio.ensure_future(_produce())
        decode_fut = loop.run_in_executor(None, _consume)
        # 任一方先结束即进入收尾：异常方触发对方取消，避免无限互等
        done, _ = await asyncio.wait(
            {produce_task, decode_fut}, return_when=asyncio.FIRST_COMPLETED
        )
        if decode_fut in done:
            de = decode_fut.exception()
            if de is not None and produce_task not in done:
                produce_task.cancel()
                try:
                    await produce_task
                except Exception:
                    pass
            if de is not None:
                raise de
        pe = produce_task.exception() if produce_task in done else None
        if pe is not None:
            # 生产者异常：等解码线程排干(EOF 已置)后退出，再抛原错
            try:
                await asyncio.wait_for(decode_fut, timeout=10.0)
            except Exception:
                pass
            raise pe
        # 生产者正常 → 等解码排干剩余缓冲
        await asyncio.wait_for(decode_fut, timeout=10.0)
        de = decode_fut.exception()
        if de is not None:
            raise de
        return None
