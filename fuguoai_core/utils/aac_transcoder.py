"""AacTranscoder — 微信手机侧 AAC(ADTS) 与 PCM(16k/mono/s16le) 的实时互转（PyAV 实现）。

relay 边界做 AAC<->PCM<->opus 三向转码。ffmpeg 原生 aac 编解码器在「管道(stdin/stdout)
模式」下都会缓冲到 EOF 才吐码流（实测：encode 持续喂帧恒 0 帧；decode 子进程不关 stdin
则输出恒为 0），故两端都用 PyAV(av) 的持久上下文逐帧实时处理：

- encode(设备下行 PCM -> AAC)：PyAV 持久编码上下文逐帧 mux，每喂一帧 PCM 立即产出
  ADTS 帧，无 EOF 依赖。AAC-LC 帧固定 1024 样本(2048B=64ms)，故把设备 opus 解出的
  640B(320 样本=20ms) 块攒到 2048B 再喂编码器；末尾不足 1024 样本时 flush()/close()
  用静音补齐，绝不丢样本。
- decode(手机上行 AAC -> PCM)：PyAV 持久解码上下文(av.codec.CodecContext)逐包喂入原始
  AAC 负载（剥 ADTS 头），解码器跨帧维护重叠相加状态，每帧精确产出 1024 样本。
  解码输出为 fltp(float32) 平面，这里用 numpy 直接转 s16le（绕开 av 重采样器会每帧
  多吐 32 样本导致 ~3% 时长膨胀的坑），保证 1:1 样本守恒。首帧 ADTS 头推导
  AudioSpecificConfig 作为解码器 extradata。

帧粒度约定（保证端到端时长一致、opus 不踩非法帧长）：
- decode 把 ADTS 帧解出的 PCM 按 640B 切成块喂 opus 编码器；不足 640B 的尾料缓在实例里
  跨帧累积，绝不把非法(非 320 整数倍)长度喂给 opus。
- 不完整 ADTS 帧在 decode 侧续传（_adts_tail），对齐/丢包不破坏切分。

异常合同：任一环节异常/失效返回空列表（调用方丢该帧），绝不抛给 relay 热路径。
"""

import io

try:
    import av
    _HAS_AV = True
except Exception:  # pragma: no cover - av 必须在运行容器内置（requirements.txt 登记）
    _HAS_AV = False

try:
    import numpy as np
    _HAS_NP = True
except Exception:  # pragma: no cover - numpy 已在 requirements.txt
    _HAS_NP = False

import logging
_logger = logging.getLogger("intercom.aac")


class AacTranscoder:
    # 设备 opus 帧 = 320 样本 = 640B(16k/mono/s16le)
    PCM_CHUNK = 640
    # AAC-LC 帧 = 1024 样本（编码器 frame_size，动态读取，这里给默认）
    AAC_FRAME_SAMPLES = 1024

    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = sample_rate
        self._dead = False
        if not _HAS_AV or not _HAS_NP:
            self._dead = True
            _logger.error(f"[AacTranscoder] 依赖缺失 av={_HAS_AV} np={_HAS_NP}，转码器不可用")
            return

        # ── decode 侧：持久解码上下文 ──
        self._pcm_leftover = b""   # 切 640B 块的尾料
        self._adts_tail = b""      # 不完整 ADTS 帧续传缓冲
        self._dec_ctx = None       # av.codec.CodecContext（首帧后创建）
        self._np = np

        # ── encode 侧：PyAV 持久编码上下文 ──
        self._enc_buf = io.BytesIO()
        try:
            self._enc_cont = av.open(self._enc_buf, mode="w", format="adts")
            self._enc_stream = self._enc_cont.add_stream("aac", rate=sample_rate)
            self._enc_stream.layout = "mono"
            try:
                self._enc_stream.bit_rate = 24000  # 与设备低码率档接近
            except Exception:
                pass
            self._aac_frame_samples = int(
                getattr(self._enc_stream, "frame_size", self.AAC_FRAME_SAMPLES)
                or self.AAC_FRAME_SAMPLES)
            self._aac_chunk = self._aac_frame_samples * 2  # 字节
        except Exception as e:
            self._dead = True
            _logger.error(f"[AacTranscoder] PyAV 编码上下文创建失败: {e}")
            return
        self._enc_leftover = b""

    # ─────────────────────────── ADTS 帧切分（前向信任）───────────────────────────
    @staticmethod
    def _split_adts(buf):
        """返回 (完整帧列表, 剩余不完整尾部)。遇到不完整末帧则留在尾部续传。"""
        frames = []
        i = 0
        n = len(buf)
        while i + 7 <= n:
            if buf[i] == 0xFF and (buf[i + 1] & 0xF0) == 0xF0:
                L = ((buf[i + 3] & 0x03) << 11) | (buf[i + 4] << 3) | ((buf[i + 5] & 0xE0) >> 5)
                if L < 7 or i + L > n:
                    break  # 末帧未到齐，留作尾部
                frames.append(buf[i:i + L])
                i += L
            else:
                i += 1
        return frames, buf[i:]

    # ─────────────────────────── decode: ADTS AAC -> 640B PCM 块 ───────────────────────────
    @staticmethod
    def _adts_header_len(frame):
        # protection_absent 在 ADTS 头中为 1 则无 CRC，帧头 7 字节；否则 9 字节
        return 7 if (frame[1] & 0x01) else 9

    @staticmethod
    def _build_asc(frame):
        """从 ADTS 头推导 AudioSpecificConfig（AAC-LC 2 字节）。"""
        profile = (frame[2] >> 6) & 0x03
        freq_idx = (frame[2] & 0x3C) >> 2
        channel = ((frame[2] & 0x01) << 2) | ((frame[3] >> 6) & 0x03)
        return bytes([(profile << 3) | (freq_idx >> 1),
                      ((freq_idx & 0x01) << 7) | (channel << 3)])

    def _ensure_decoder(self, frame):
        if self._dec_ctx is not None:
            return True
        try:
            asc = self._build_asc(frame)
            ctx = av.codec.CodecContext.create("aac", "r")
            ctx.extradata = asc
            self._dec_ctx = ctx
            return True
        except Exception as e:
            _logger.warning(f"[AacTranscoder.decode] 解码器初始化失败: {e!r}")
            return False

    def _decode_frame(self, frame):
        """解码单帧 ADTS -> PCM(s16le) 字节。返回 b'' 表示失败/无输出。"""
        hl = self._adts_header_len(frame)
        if len(frame) <= hl:
            return b""
        try:
            pkt = av.Packet(frame[hl:])
            out = b""
            for fr in self._dec_ctx.decode(pkt):
                arr = fr.to_ndarray()  # (channels, samples) float32
                s16 = self._np.clip(arr * 32767.0, -32768, 32767).astype("<i2")
                out += s16.tobytes()
            return out
        except Exception as e:
            _logger.warning(f"[AacTranscoder.decode] 单帧解码失败(丢帧): {e!r}")
            return b""

    def _flush_decoder(self):
        """解码器 flush，取回末尾被重叠相加持有的帧。返回 PCM(s16le) 字节。"""
        out = b""
        try:
            for fr in self._dec_ctx.decode(None):
                arr = fr.to_ndarray()
                s16 = self._np.clip(arr * 32767.0, -32768, 32767).astype("<i2")
                out += s16.tobytes()
        except Exception:
            pass
        return out

    def decode(self, aac_frame: bytes):
        """喂一帧/数帧 ADTS AAC（手机上行），返回可用 PCM 块列表（每块 640B=20ms@16k）。
        缓冲不足/失效返回空列表（调用方丢该帧）。"""
        if self._dead or not aac_frame:
            return []
        self._adts_tail += aac_frame
        complete, self._adts_tail = self._split_adts(self._adts_tail)
        out_pcm = b""
        for f in complete:
            if not self._ensure_decoder(f):
                continue
            out_pcm += self._decode_frame(f)
        if not out_pcm:
            return []
        self._pcm_leftover += out_pcm
        chunks = []
        while len(self._pcm_leftover) >= self.PCM_CHUNK:
            chunks.append(self._pcm_leftover[:self.PCM_CHUNK])
            self._pcm_leftover = self._pcm_leftover[self.PCM_CHUNK:]
        return chunks

    # ─────────────────────────── encode: 640B PCM -> ADTS AAC 帧 ───────────────────────────
    def _encode_one(self, pcm_block: bytes) -> bytes:
        """喂恰好 self._aac_frame_samples 样本的 PCM，返回该帧 ADTS 字节。"""
        try:
            frame = av.AudioFrame(format="s16", layout="mono", samples=self._aac_frame_samples)
            frame.sample_rate = self.sample_rate
            frame.planes[0].update(pcm_block)
            pos = len(self._enc_buf.getbuffer())
            for packet in self._enc_stream.encode(frame):
                self._enc_cont.mux(packet)
            return bytes(self._enc_buf.getbuffer()[pos:])
        except Exception as e:
            _logger.warning(f"[AacTranscoder.encode] 单帧编码失败(丢帧): {e!r}")
            return b""

    def encode(self, pcm: bytes):
        """喂一帧 640B PCM（设备下行转码中间产物），返回可用 ADTS 帧列表。
        缓冲不足/失效返回空列表（调用方丢该帧）。"""
        if self._dead or not pcm:
            return []
        self._enc_leftover += pcm
        frames = []
        while len(self._enc_leftover) >= self._aac_chunk:
            chunk = self._enc_leftover[:self._aac_chunk]
            self._enc_leftover = self._enc_leftover[self._aac_chunk:]
            adts = self._encode_one(chunk)
            if adts:
                frames.append(adts)
        return frames

    def flush(self):
        """把 encode 侧剩余不足一帧的尾料用静音补齐后编码，返回 ADTS 帧列表。
        会话结束/最后一次数据后调用，避免丢末尾样本。"""
        if self._dead:
            return []
        if not self._enc_leftover:
            return []
        pad = self._aac_chunk - len(self._enc_leftover)
        block = self._enc_leftover + b"\x00" * pad
        self._enc_leftover = b""
        adts = self._encode_one(block)
        return [adts] if adts else []

    def close(self):
        if self._dead and not getattr(self, "_enc_cont", None) and self._dec_ctx is None:
            return
        self._dead = True
        # encode 尾部补齐并刷新编码器（末帧在此产出，relay 在会话结束前已陆续下发，
        # 仅最后 ~64ms 随会话拆除而丢失，可接受）
        try:
            self.flush()
        except Exception:
            pass
        try:
            for packet in self._enc_stream.encode(None):
                self._enc_cont.mux(packet)
        except Exception:
            pass
        try:
            self._enc_cont.close()
        except Exception:
            pass
        # decode 侧 flush 取回末尾帧（relay 不再读取，丢弃，仅避免资源泄漏）
        try:
            self._flush_decoder()
        except Exception:
            pass
        self._dec_ctx = None
        self._adts_tail = b""
        self._pcm_leftover = b""
