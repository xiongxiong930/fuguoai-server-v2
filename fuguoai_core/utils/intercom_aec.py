# -*- coding: utf-8 -*-
"""对讲服务端 AEC（B2 全双工）引擎。

两种上行模式（由 hello 协商的 intercom.server_aec_mode 决定）：
  - "2ch"    ：固件 hello 收到后端 aec_ready 后切 2ch[mic|ref] 交织上行（Design B）。
               本模块 2ch 解码 → 解交织 mic(ch0)/ref(ch1) → 按**到达序 FIFO** 取上一帧
               ref 作回声参考（设备声学回声延迟实测恒定 1 帧=20ms，故回声源恒为 hist[-2]），
               直接时域回声对消（相关搜索在真实连续语音中极易误选近端帧→劣于同帧，已弃用）。
               不依赖墙钟 ts（真机突发发帧会致 ts 键碰撞覆盖历史→旧实现啸叫）。
  - "outband"（默认，适配 1ch 单声道板 bread-compact-wifi = NoAudioCodecSimplex/Duplex，
               无硬件 codec，固件只能 mono 上行）：
               固件维持 mono 上行；回声参考由**后端自喂**——relay 把下行(→设备)的
               peer 音频解码为 16k PCM，按服务端发送时刻存入 registry 的 ref 缓存
               （与 chat 自喂参考同源思路，不依赖固件 audio_playback_ref）。
               本模块 mono 解码上行 mic → 取 out-of-band ref 缓存 → **移植 chat _apply_aec
               的互相关时延估计**（[-30, max_delay_ms] 窗口，覆盖 40~60ms 声学路径；
               90C0 的 5~9s DMA 偏移靠 max_delay_ms>=8000）→ 时域回声对消。

设计要点
--------
- 复用 chat 已验证的 numpy 回声对消思路（connection._apply_aec 同款），但**独立成模块**，
  不改动生产中的 _apply_aec（P0，保持零回归风险）。
- outband 与 chat 的关键差异：ref 来自后端自喂的下行 peer 音频（而非固件参考流），
  mic 上行是 mono（非 2ch 交织），故必须做时延搜索才能对齐 ref 与 mic。
- 默认不启用：由 hello 的 server_aec 开关 + server_aec_devices 灰度名单控制；
  本模块纯计算，不污染任何全局状态，实例按 device_id 生命周期创建/销毁。

与自进化路线关系
----------------
- 把"消回声"这一智力从设备端（本板 RAM 装不下 esp_aec）挪到**我们的后端**，让后端成为
  差异点；这是复果后端自研的对讲增强能力，属我们真正自研、构成差异化竞争力的 IP。
"""

import time
import os
import struct
import numpy as np
import opuslib_next
import logging

_logger = logging.getLogger(__name__)

SR = 16000
FRAME_MS = 20
FRAME_SAMPLES = SR * FRAME_MS // 1000  # 320 @16k/20ms

# 解码缓冲上限（与 intercom_registry 既有 2880 常量一致：覆盖 40~60ms@16k 固件帧）
_DECODE_MAX = 2880

# 参考静默 / mic 静默门限（与 chat _apply_aec 一致）
_REF_RMS_FLOOR = 50
_MIC_RMS_FLOOR = 30


class IntercomAec:
    """单设备服务端 AEC 实例（有状态：opus 编解码 + 时域回声对消增益平滑）。

    mode="2ch"    ：上行 2ch 交织[mic|ref]，按到达序 FIFO 取上一帧 ref 作回声参考 + 时域互相关选帧。
    mode="outband"：上行 mono；ref 取自后端自喂缓存（ts→pcm 字典），做互相关时延估计。
    """

    def __init__(self, sample_rate: int = SR, frame_ms: int = FRAME_MS,
                 mode: str = "outband", device_id: str = "",
                 max_delay_ms: int = 2000,
                 echo_gain_cap: float = 0.8,
                 echo_delay_ms: int = 0, echo_delay_band_ms: int = 0,
                 diag: bool = False,
                 multi_tap: bool = False, tap_len_ms: int = 120,
                 nlms_mu: float = 0.2, nlms_w_cap: float = 0.95,
                 dtd_ratio: float = 0.3,
                 residual_suppress: bool = True, residual_floor: float = 0.1,
                 auto_tune: bool = False, ner_auto: bool = False,
                 ner_outband: bool = False,
                 phone_align_corr_min: float = 0.3,
                 phone_misalign_floor: float = 0.05):
        # ── ② AEC 在线自适应（NLMS 抽头/μ 寻优，默认关）──
        # 仅当 multi_tap 启用且 auto_tune=True 时生效；基于实例内 diag_summary 的
        # corr.mean/weak_count 信号小幅调整 tap_len/nlms_mu，带上下限钳制，绝不发散。
        # 默认关闭：生产零影响，需真机验收后才开。
        self._auto_tune = bool(auto_tune)
        self._auto_tap_min_ms = 60
        self._auto_tap_max_ms = 400
        self._auto_mu_min = 0.05
        self._auto_mu_max = 0.3
        self._auto_lr = 0.1          # 每步调整比例（保守）
        self._auto_step_min_samples = 200   # 至少累积这么多 diag 样本才调
        # ── ③ NER 门控自调（默认关）──
        # 仅 2ch_srvref 模式 + ner_auto=True 时生效；基于实例内 ref_active_rate 做
        # conservative sanity + residual_floor 微调框架。跨层啸叫信号(watchdog teardown)
        # 在 call_store 层，本期不跨层耦合，留 TODO。默认关闭。
        self._ner_auto = bool(ner_auto)
        self._ner_floor_min = 0.05
        self._ner_floor_max = 0.4
        # ④ phone 腿 NER 开关（2026-09-08 8818 环路模型）：8818 啸叫环必须经手机腿维持
        # （实证：手机静音啸叫即变小），设备腿 NER 0.1 仍不够 → 手机腿(outband)也要压。
        # 默认 False 不改变既有 outband 行为；由 config aec_phone_residual_suppress 开启。
        # ⚠️ phone 门控看最近 1500ms ref（2026-09-11 修正：原 300ms 过窄，手机软件播放器外放延迟≤1s 致回声源落窗外→啸叫；2ch_srvref 的 full-window 是 90C0 深延迟语义，phone 不适用但窗须覆盖外放延迟）。
        self._ner_outband = bool(ner_outband)
        # phone 腿对齐置信门控（2026-09-13 修复手机间歇啸叫+双端声音小）：
        # 手机软件播放器外放+回采有 ~1.4s 抖动，best_ref 频繁误选帧→单抽头
        # g=<mic,ref>/<ref,ref> 估错→误削近端(双端声音小) 或 误留回声(啸叫)。
        # best_corr<phone_align_corr_min 视为对齐失败：process_ref_cache 跳过线性谱减，
        # 改由 phone_misalign_floor 强抑制地板直接压上行断环(α×floor<1)，既不断环
        # 也不误削近端。仅 outband 腿生效，2ch_srvref(8818 健康腿) 走原路径零回归。
        self._phone_align_corr_min = float(phone_align_corr_min)
        self._phone_misalign_floor = float(phone_misalign_floor)
        self.sr = sample_rate
        self.samples = sample_rate * frame_ms // 1000
        self.mode = mode
        # device_id：诊断抓包按设备分文件（2026-09-11 抓包污染复盘修正）
        self.device_id = device_id or ""
        # 回声增益上限：声学回声路径增益(扬声器→mic 耦合)通常 0.3~0.7。
        # 谱减的 Wiener 增益会被"近端/ref 频谱相似"撑大 → 把近端也当回声削掉。
        # 钳到回声增益上限可保住近端主导频带，同时回声主导频带仍被消。默认 0.8 留余量。
        self._echo_gain_cap = float(echo_gain_cap)
        # 时延搜索窗口上限：默认 2000(覆盖 40~300ms 声学路径)；90C0 出声 DMA 深，
        # 回声比参考晚到 5~9s，部署须配 aec_max_delay_ms>=8000。
        self.max_delay_ms = max(2000, int(max_delay_ms))
        # 回声延迟带约束（仅 90C0 等深 DMA 缓冲长延迟设备需要，治本啸叫根因）：
        # 90C0 出声 DMA 深，回声比参考晚到 5~9s。在 [-30, max_delay_ms] 全窗口做单帧
        # 归一化互相关搜索，跨 12s 数百帧语音极易**误选近期近端相似帧**(而非真实延迟
        # 回声帧)→ 回声未消 → 反馈啸叫。echo_delay_ms>0 时把候选窗口收窄到
        # [echo_delay_ms - band, echo_delay_ms + band]，使 _td_corr 只能在真实回声延迟
        # 带内选帧，彻底避免误选。echo_delay_ms=0(默认) 保持全窗口，兼容 phone/短延迟设备。
        self._echo_delay_ms = int(echo_delay_ms)
        self._echo_delay_band_ms = int(echo_delay_band_ms)
        # 诊断：回声延迟实测（由 config intercom_aec_diag 控制，默认关）。
        # 开启后 process_ref_cache 每帧把估计回声延迟(mic_ts - best_ts, ms)与互相关峰
        # 强度(best_corr)记入 _diag_samples，disable 时输出统计——用于实测 90C0 等
        # 设备的真实声学回声延迟 / 方差 / 线性度（替代凭记忆猜的"5~9s"）。
        self._diag = bool(diag)
        self._diag_samples = []      # [(delay_ms, corr), ...]
        self._diag_collected = False
        self._diag_max = 6000        # 上限 ~120s@50fps，防内存膨胀
        # ── ERLE 观测（mode="2ch" 分支；对方联调纪要三问①要求的 echo_return_loss 统计）──
        # 每帧记 (mic_rms, out_rms)，ERLE=20*log10(mic_rms/out_rms)：后端 AEC 实际抑制量。
        # 滑窗节流 5s 打一条（含中位），disable 时 diag_summary 汇总——与设备 [HOWL-AGC]
        # 增益做双侧对照：ERLE≥20dB → 后端补足缺口；ERLE≈0 → 参考同源失败。
        self._erle_samples = []      # [(mic_rms, out_rms, erle_db), ...]
        self._erle_last_log = 0.0
        # ── 多抽头 NLMS 自适应滤波（替代单抽头谱减，消房间混响 smear）──
        # 单抽头只估一个标量增益 g=<mic,ref>/<ref,ref>，仅能消"纯延迟+增益"回声；
        # 真实回声路径是 ref 经房间冲激响应 h(t)(多抽头，含直达+反射混响)卷积，
        # 单抽头消不掉混响尾巴→残留~55%(corr=0.45)。多抽头 NLMS 学整个 h，
        # 把回声估计从 g·ref 升级为 w·ref_window(w 为 tap_len 抽头 FIR)，彻底消除线性回声。
        # 前置：bulk delay 须由互相关准确选出(已有 _td_corr 选 best_ts)；延迟抖动大
        # (每帧 best_ts 漂移)时 FIR 基准跳→收敛受扰，故加延迟锁定(EMA 平滑)。
        self._multi_tap = bool(multi_tap)
        self._tap = max(64, int(tap_len_ms * sample_rate // 1000))  # FIR 抽头数(混响尾巴)
        self._nlms_mu = float(nlms_mu)
        self._nlms_w_cap = float(nlms_w_cap)
        self._dtd_ratio = float(dtd_ratio)
        # ── 非线性回声抑制(NER)兜底 ──
        # 90C0 用 2ch_srvref 全双工时，本机固件 ch1 坏、mic 不静音、且扬声器出声有 5~9s
        # 秒级抖动的播放缓冲延迟，致使线性 AEC 每帧对齐锚点漂移、消不净→「扬声器→mic」
        # 正反馈环啸叫。NER 不依赖延迟对齐：以「远端是否在讲话(ref_active)」门控，只要
        # 远端在放回声源就固定衰减(residual_floor)mic 上行，把环路增益硬压到 <1，彻底断环。
        # 类比经典回声抑制器(非 AEC)，是全双工消回声的最后一道保险；仅 2ch_srvref 启用。
        self._residual_suppress = bool(residual_suppress)
        self._residual_floor = float(residual_floor)
        self._w = np.zeros(self._tap, dtype=np.float32)   # 自适应 FIR 权重(回声路径估计)
        self._locked_delay = None   # 延迟锁定(ms)，EMA 平滑 best_delay 抗抖动
        # ── NER 门控 hangover + 命中率诊断 ──
        # ref_active 只看「当前帧窗口内 ref 是否有能量」。peer 说话有自然停顿→停顿帧
        # ref_active=False→mic 原样过→此刻房间仍在响的回声尾巴经「扬声器→mic」漏回→
        # 间断啸叫。hangover：ref 活跃后保持衰减 ~1.5s，覆盖说话停顿与房间混响尾巴，断环。
        self._ref_active_hold_until = 0
        self._ref_active_count = 0   # 2ch_srvref 下被衰减(门控生效)的帧数
        self._ref_active_total = 0   # 2ch_srvref 下进入门控判定的帧数
        self._ref_active_hold_ms = 1500
        # 上行解码：2ch / 2ch_srvref 解 2ch 交织(固件 aec_ready 后发 2ch)；
        # outband 解 mono。2ch_srvref 仅借 2ch 通道拿全双工 mic，ch1(ref) 在本机
        # 固件为空→丢弃，回声参考改走服务端自喂(下行播放)，见 process_opus。
        channels = 2 if mode in ("2ch", "2ch_srvref") else 1
        self._dec = opuslib_next.Decoder(sample_rate, channels)
        # 下行 mono 编码（对端/手机只需干净单声道）
        self._enc = opuslib_next.Encoder(sample_rate, 1, opuslib_next.APPLICATION_VOIP)
        try:
            self._enc.bitrate = 24000  # 与设备低码率档接近，控制下行带宽
        except Exception:
            pass
        # 2ch 回声参考历史：按**到达顺序**的 FIFO（int16 数组），与设备帧序严格一致。
        # 设备声学回声延迟实测为恒定 1 帧(320 样本=20ms)，故回声源永远是"上一帧的 ref"。
        # 早期实现用 time.time()*1000 做字典键存历史，但真机成窗突发发帧时多帧落在同一
        # 毫秒→ts 键碰撞→ref 历史被覆盖→找不到正确回声帧→谱减等于没做→啸叫。FIFO 不
        # 依赖墙钟，每帧 append 即可永远拿到 hist[-2]，彻底免疫该碰撞。
        self._ref_hist_max = 32    # 保留最近 32 帧(≈640ms)，足够覆盖 1 帧延迟+静默兜底
        self._ref_hist = []        # 到达序 FIFO: int16 ref 数组

    # ── 帧级 API ──────────────────────────────────────────────

    @staticmethod
    def _deinterleave(pcm_2ch: bytes):
        """2ch 交织 int16 → (mic, ref) 两路 int16 数组（与 process 的 int16 契约一致）。

        注意：必须返回 int16 字节布局，process() 内部按 int16 重新解析；
        若返回 float32，tobytes() 长度翻倍会被误读为双倍样本 → 输出帧长错乱。
        """
        arr = np.frombuffer(pcm_2ch, dtype=np.int16)
        if arr.size % 2 != 0:
            arr = arr[:-1]
        mic = arr[0::2].astype(np.int16)
        ref = arr[1::2].astype(np.int16)
        return mic, ref

    @staticmethod
    def _td_corr(audio1: np.ndarray, audio2: np.ndarray) -> float:
        """时域归一化互相关（波形余弦相似度），用于从 ref 缓存里挑出与当前 mic 最匹配的参考帧。

        回声是 ref 的**延迟缩放副本**：含回声的 mic 与目标 ref 帧波形高度相似（尖锐峰），
        而相邻语音帧波形相似度低。相比 log-PSD（只比频谱形状，真实语音各帧频谱近似 →
        无法区分真正的回声帧，会选错帧 → 谱减不减回声甚至放大 → 反馈啸叫），时域互相关
        能在频谱相似的真实语音中稳定锁定回声参考帧。返回 [-1,1]，1=完全同向。
        """
        n = len(audio1)
        if n == 0 or n != len(audio2):
            return -1.0
        e1 = float(np.dot(audio1, audio1))
        e2 = float(np.dot(audio2, audio2))
        if e1 < 1e-6 or e2 < 1e-6:
            return -1.0
        return float(np.dot(audio1, audio2) / np.sqrt(e1 * e2 + 1e-8))

    def process(self, mic_pcm: bytes, ref_pcm: bytes) -> bytes:
        """给定同帧(已对齐) mic/ref PCM(int16)，返回消回声后的 mono PCM(int16)。

        参考静默（无人声播放）或 mic 静默：无需消回声，原样返回（零开销、零损伤）。
        2ch 模式(Δ≈0)使用本方法。
        """
        mic = np.frombuffer(mic_pcm, dtype=np.int16).astype(np.float32)
        if mic.size == 0:
            return mic_pcm
        ref = np.frombuffer(ref_pcm, dtype=np.int16).astype(np.float32)
        mic_rms = float(np.sqrt(np.mean(mic ** 2)))
        ref_rms = float(np.sqrt(np.mean(ref ** 2)))
        if ref_rms < _REF_RMS_FLOOR or mic_rms < _MIC_RMS_FLOOR:
            return mic_pcm
        return self._spectral_subtract(mic, ref)

    def _spectral_subtract(self, mic: np.ndarray, ref: np.ndarray,
                           corr: float = None, scale_cap: float = None) -> bytes:
        """时域最小二乘回声对消，返回 int16 mono PCM。

        回声是 ref 的**延迟缩放线性副本**：mic = 近端(与 ref 不相关) + g·ref(回声)。
        取投影增益 g = ⟨mic,ref⟩ / ⟨ref,ref⟩ 直接在**时域**减 g·ref，
        只去除 mic 中与 ref 线性相关(回声)的分量、完好保留与 ref 正交的近端真声；
        **无 FFT 块边界毛刺、无 musical noise**（旧频域幅度谱减的两大失真源）。
        双端（90C0 与 phone 的 outband 路径）共用本方法，均受益。

        g 钳到 [0, 上限]：回声路径增益(扬声器→mic 耦合)通常 0.3~0.7；钳上限保住
        近端主导频带、回声主导频带仍被消。scale_cap=None 时仅钳 g>=0（兼容 2ch
        同帧 process() 调用，原契约即不过减）。
        """
        # 参考能量（回声路径增益估计分母）；参考静默则无回声可消
        ref_energy = float(np.dot(ref, ref))
        if ref_energy < 1e-3:
            # 无回声：原样返回（量化回 int16，零损伤）
            return mic.astype(np.int16).tobytes()

        # 投影增益（最小二乘最优单抽头）：g = <mic,ref> / <ref,ref>
        g = float(np.dot(mic, ref)) / (ref_energy + 1e-8)
        # 仅取正向耦合（回声是 ref 的正数缩放副本，负向无物理意义）
        if g < 0:
            g = 0.0
        # 钳到回声增益上限：防"近端/ref 频谱相似"把增益撑大→误削近端
        cap = scale_cap if scale_cap is not None else float('inf')
        if g > cap:
            g = cap

        # 直接在时域减去回声相关分量：稳态下回声路径增益恒定，逐帧投影增益 g 已平滑，
        # 无需跨帧滤波（避免首帧欠消与回声起始响应滞后）。残差即与 ref 正交的近端真声。
        output = mic - g * ref
        output = np.clip(output, -32768, 32767)
        return output.astype(np.int16).tobytes()

    def _multitap_cancel(self, mic: np.ndarray, best_ts: int, ref_cache: dict,
                         mic_ts: int) -> bytes:
        """多抽头 NLMS 自适应回声消除（替代单抽头谱减）。

        以互相关选出的 bulk delay(best_ts) 为基准，从 ref_cache 重建连续 ref 流，
        学出回声路径 FIR w(tap_len 抽头)，对每帧 mic 做 y=w·ref_window 估计回声、
        e=mic-y 输出残差（近端）。双讲(DTD)冻结更新防学近端；延迟锁定(EMA)抗抖动。
        向量化(n×tap 滑动窗口矩阵乘)，后端算力充足可承受。

        返回 int16 mono PCM；任一前置不满足(无连续 ref/窗口不匹配)→None 交由调用方降级。
        """
        n = mic.size
        if n == 0:
            return mic.astype(np.int16).tobytes()
        # 延迟锁定：EMA 平滑 best_delay，避免每帧 best_ts 漂移扰动 FIR 基准。
        delay = mic_ts - best_ts
        if self._locked_delay is None:
            self._locked_delay = float(delay)
        else:
            self._locked_delay = 0.9 * self._locked_delay + 0.1 * delay
        base_ts = int(mic_ts - self._locked_delay)
        # 重建连续 ref 流：从 base_ts 起拼接 PCM，长度需 >= tap+n（滑窗覆盖）。
        need = self._tap + n
        sorted_ts = sorted(ref_cache.keys())
        chunks = []
        got = 0
        for ts in sorted_ts:
            if ts < base_ts:
                continue
            chunk = np.frombuffer(ref_cache[ts], dtype=np.int16).astype(np.float32)
            chunks.append(chunk)
            got += chunk.size
            if got >= need:
                break
        if not chunks:
            return None
        ref_stream = np.concatenate(chunks)[:need]
        if ref_stream.size < need:
            ref_stream = np.pad(ref_stream, (0, need - ref_stream.size))
        # 滑动窗口矩阵 R[i,k]=ref_stream[i+k]，shape (n,tap)；y=R@w 为回声估计。
        idx = np.arange(self._tap)[None, :] + np.arange(n)[:, None]
        R = ref_stream[idx]                                    # (n, tap)
        if R.shape != (n, self._tap):
            return None
        y = R @ self._w                                        # (n,)
        e = mic - y
        ref_energy = float(np.mean(R * R) + 1e-8)
        mic_energy = float(np.mean(mic * mic) + 1e-8)
        e_energy = float(np.mean(e * e) + 1e-8)
        # 双讲检测(DTD)：残差能量远大于 mic(近端主导)→冻结更新；ref 过弱也无回声可学。
        dtd_freeze = (e_energy > mic_energy * self._dtd_ratio) or (ref_energy < 1.0e3)
        if not dtd_freeze:
            grad = np.mean(e[:, None] * R, axis=0) * (self._nlms_mu / ref_energy)
            self._w = np.clip(self._w + grad.astype(np.float32),
                              -self._nlms_w_cap, self._nlms_w_cap)
        out = np.clip(e, -32768, 32767).astype(np.int16)
        return out.tobytes()

    def process_ref_cache(self, mic_pcm: bytes, ref_cache: dict, mic_ts: int,
                           max_delay_ms: int = None) -> bytes:
        """mono mic PCM + out-of-band ref 缓存(服务端时钟 ts→pcm 字典) → 消回声 mono PCM。

        从缓存里挑候选窗口内相关度最高的参考帧（互相关时延估计），对齐后谱减。
        候选窗口默认 [-30ms, +max_delay_ms]；当实例 echo_delay_ms>0（长延迟设备如 90C0）
        收窄到 [echo_delay_ms - band, echo_delay_ms + band]，避免误选近端相似帧致啸叫。
        无候选 / 参考静默 / mic 静默 → 原样返回（零损伤降级）。

        max_delay_ms 可覆盖（2ch 声学路径用较小窗口，避免误匹配与内存膨胀）。
        """
        mic = np.frombuffer(mic_pcm, dtype=np.int16).astype(np.float32)
        if mic.size == 0:
            return mic_pcm
        mic_rms = float(np.sqrt(np.mean(mic ** 2)))
        if mic_rms < _MIC_RMS_FLOOR:
            return mic_pcm
        if not ref_cache:
            return mic_pcm

        _max_d = max_delay_ms if max_delay_ms is not None else self.max_delay_ms
        n = mic.size
        sorted_ts = sorted(ref_cache.keys())
        # 参考经声学路径(+设备 DMA 缓冲 + 网络抖动)晚于 mic 帧到达，故参考 ts 应早于 mic ts。
        # 默认窗口 [-30, _max_d]。
        _lo = -30.0
        _hi = float(_max_d)
        if self._echo_delay_ms > 0:
            # 长延迟设备(90C0 回声 5~9s)：把候选窗口约束到已知回声延迟带，避免互相关
            # 在 12s 全窗口内误选近期近端相似帧 → 回声未消 → 啸叫。band 缺省 2000ms，
            # 保证窗口至少 4s 宽、覆盖延迟波动。
            _band = self._echo_delay_band_ms if self._echo_delay_band_ms > 0 else 2000
            _lo = max(-30.0, float(self._echo_delay_ms) - _band)
            _hi = float(self._echo_delay_ms) + _band
        candidates = [ts for ts in sorted_ts
                      if _lo <= (mic_ts - ts) <= _hi]
        if not candidates:
            return mic_pcm

        best_corr = -1.0
        best_ts = candidates[0]
        ref_active = False   # 窗口内任一 ref 帧有能量 = 远端在讲话 = 设备扬声器在放回声源
        for cts in candidates:
            test_ref = np.frombuffer(ref_cache[cts], dtype=np.int16).astype(np.float32)
            if np.sqrt(np.mean(test_ref ** 2)) >= _REF_RMS_FLOOR:
                ref_active = True
            else:
                continue
            c = self._td_corr(mic, test_ref)
            if c > best_corr:
                best_corr = c
                best_ts = cts

        # NER hangover：ref 活跃后保持衰减 _ref_active_hold_ms，覆盖 peer 说话自然停顿与
        # 房间混响尾巴；否则停顿帧 ref_active=False→mic 原样过→回声尾巴漏回→间断啸叫。
        if ref_active:
            self._ref_active_hold_until = mic_ts + self._ref_active_hold_ms
        ref_active_eff = bool(ref_active) or (mic_ts < self._ref_active_hold_until)
        if self.mode == "2ch_srvref":
            self._ref_active_total += 1
            if ref_active_eff:
                self._ref_active_count += 1

        best_ref = np.frombuffer(ref_cache[best_ts], dtype=np.int16).astype(np.float32)
        ref_rms = float(np.sqrt(np.mean(best_ref ** 2)))
        if ref_rms < _REF_RMS_FLOOR:
            return mic_pcm

        # 诊断：记录每帧最佳互相关峰(不限 0.3 门槛)。原 0.3 门槛是为过滤 outband 闭麦
        # 期的噪声伪峰而设，但 2ch_srvref mic 常开时弱回声(<0.3)会被整批过滤→完全失明。
        # 去掉门槛后延迟估计(mic_ts-best_ts)与 corr 分布都能看到；summary 仍按 0.5 区分
        # strong/weak，便于判读。纯诊断、不改 AEC 行为；可逆、不固化。
        if self._diag:
            self._record_diag(int(mic_ts - best_ts), best_corr)

        # phone 腿对齐置信门控（2026-09-13 修复手机间歇啸叫+双端声音小）：
        # 手机软件播放器外放+回采有 ~1.4s 抖动，best_ref 频繁误选帧→单抽头
        # g=<mic,ref>/<ref,ref> 估错→误削近端(双端声音小) 或 误留回声(啸叫)。
        # best_corr<门槛视为对齐失败：跳过线性谱减，改由下方 misalign 强抑制地板
        # 直接压上行断环(α×floor<1)，既不断环也不误削近端。仅 outband 腿生效，
        # 2ch_srvref(8818 健康腿) 走原路径零回归。
        _phone_misaligned = (self.mode == "outband"
                             and best_corr < self._phone_align_corr_min)
        if _phone_misaligned:
            out = (np.clip(mic * self._phone_misalign_floor, -32768, 32767)
                   .astype(np.int16).tobytes())
        # 多抽头 NLMS：以 bulk delay(best_ts) 为基准学 FIR 消房间混响尾巴(替代单抽头)。
        # 失败/窗口不匹配时 None 降级到下方单抽头谱减，保证零回归。
        elif self._multi_tap:
            mt = self._multitap_cancel(mic, best_ts, ref_cache, mic_ts)
            if mt is not None:
                out = mt
            else:
                aligned_ref = best_ref[:n]
                if aligned_ref.size < n:
                    aligned_ref = np.pad(aligned_ref, (0, n - aligned_ref.size))
                out = self._spectral_subtract(mic, aligned_ref, corr=best_corr,
                                              scale_cap=self._echo_gain_cap)
        else:
            aligned_ref = best_ref[:n]
            if aligned_ref.size < n:
                aligned_ref = np.pad(aligned_ref, (0, n - aligned_ref.size))
            out = self._spectral_subtract(mic, aligned_ref, corr=best_corr,
                                          scale_cap=self._echo_gain_cap)

        # 非线性回声抑制(NER)兜底：以「远端是否在讲话(ref_active)」门控、固定衰减 mic 上行，
        # 断掉「扬声器→mic」正反馈环，保证环路增益恒<1 彻底杜绝啸叫（经典回声抑制器做法，
        # 不依赖精确线性对消、不依赖延迟对齐，对 90C0 的 5~9s 秒级抖动完全免疫）。
        # **仅 2ch_srvref 全双工启用**：本机固件 ch1 坏、mic 不静音、秒级延迟抖动致线性 AEC
        # 消不净→反馈啸叫，必须用 NER 断环。outband(半双工 mic 静音 / phone) 无反馈环风险，
        # 不启用 NER，保住近端真声(对讲要把设备用户声音传给 peer)。
        if self.mode == "2ch_srvref":
            out = self._apply_residual_suppress(out, ref_active_eff, mic_rms)
        elif self.mode == "outband" and self._ner_outband and not _phone_misaligned:
            # phone 腿 NER（ner_outband 开启时）：门控看最近 1500ms 的 ref
            # （下行→手机 = 设备端正在出声，含啸叫），压手机上行断环路手机腿。
            # 与 2ch_srvref 的 full-window 门控（90C0 深延迟语义）刻意不同。
            # ⚠️ 2026-09-11 修正：原窗 300ms 太窄。手机是软件播放器，小程序
            #   audio-player.js MAX_BUFFER_S=1.0 抖动缓冲使外放延迟 0~1s，回采上传
            #   mic_ts 比被回放设备语音 ts 晚近 1s → 抖动堆积时回声源落 300ms 窗外
            #   → NER 漏判 → 环路增益不压<1 → 手机啸叫。放宽到 1500ms 覆盖手机
            #   实际环路时延(下行网络+外放≤1s+回采+上行网络≈1.4s)，配合已有
            #   _ref_active_hold_ms=1500 兜底，持续讲话也不断环。
            _ref_active_phone = False
            for _ts in sorted_ts:
                if mic_ts - 1500 <= _ts <= mic_ts + 100:
                    _r = np.frombuffer(ref_cache[_ts], dtype=np.int16).astype(np.float32)
                    if _r.size and float(np.sqrt(np.mean(_r ** 2))) >= _REF_RMS_FLOOR:
                        _ref_active_phone = True
                        break
            if _ref_active_phone:
                self._ref_active_hold_until = mic_ts + self._ref_active_hold_ms
            _phone_eff = _ref_active_phone or (mic_ts < self._ref_active_hold_until)
            self._ref_active_total += 1
            if _phone_eff:
                self._ref_active_count += 1
            out = self._apply_residual_suppress(out, _phone_eff, mic_rms)
        # ② AEC 在线自适应 / ③ NER 自调：基于实例内 diag 信号做保守小幅调整。
        # 默认关（auto_tune/ner_auto=False），且必须 diag 开启且样本足够才动作，零生产影响。
        if self._diag and (self._auto_tune or self._ner_auto):
            self._auto_tune_step()
        # 诊断：落盘**已对齐**的 (mic, ref, out) 三元组（flag 触发，零开销）。
        # 供离线 AEC benchmark 用真实配对数据（见 aec_bench/），默认不产生任何 IO。
        self._maybe_capture_pair(mic, best_ref, out)
        return out

    def _maybe_capture_pair(self, mic: np.ndarray, ref: np.ndarray, out_pcm):
        """诊断：落盘**已对齐**的 (mic, ref, out) 三元组，供离线 AEC benchmark 使用。

        与 intercom_raw2ch.pcm 的区别（关键）：那份是 mode="2ch" 设备上行 ch1 的实录，
        实测 ch1 是低频杂散(谱峰 63.5Hz、纯远端活动帧=0) —— 记录的是「设备 ch1 未馈入
        播放 PCM」的**坏态**，做 benchmark 输入会让任何算法都表现糟糕且无法归因。
        这里落的是 **服务端自喂下行参考经互相关选帧后的真正回声源(best_ref)** + 设备
        上行 mic + 本帧输出 out，即神经 AEC(JEAC/DFSMN) 要吃的同一组输入。

        由 flag 文件 /opt/fuguoai-server/data/tmp/.intercom_pair 触发（每个 device_id
        一个文件）；无 flag 时本函数只做一次 os.path.exists 检查、零开销。上限 8MB(≈7 分钟
        20ms 帧)防写爆。落盘 magic INTCPAIR + <IHH sr, ch=3, mode_code> + 3ch 交织 int16
        [mic, ref, out]；mode_code: 1=2ch_srvref 2=outband。
        ⚠️ 2026-09-11 抓包污染复盘：原实现写单一 intercom_pair.pcm，头 mode_code 由首个
        写入实例锁定；一次对讲含 phone 腿(outband)+设备腿(2ch_srvref) 两个 IntercomAec
        实例，二者帧会混入同一文件、共用 phone 腿 outband 头 ⇒ 腿混淆、二值定案读数失真。
        现改为每实例写 intercom_pair_<device_id>.pcm（device_id 即 hello 协商的腿标识），
        头 mode_code 反映本腿真实模式，腿间互不污染。离线 pair_verdict.py 指定设备腿文件即得
        干净读数。
        """
        _flag = "/opt/fuguoai-server/data/tmp/.intercom_pair"
        try:
            if not os.path.exists(_flag):
                return
            _arm = os.path.getmtime(_flag)
        except Exception:
            return
        # 自动 disarm（2026-09-12）：arming 超过 10 分钟未 disarm 视为遗忘，停写并移除 flag。
        # 防 2026-09-11 事故重演——flag 常 armed 致多通通话帧拼进同一文件(8MB 封顶混合)。
        try:
            if (time.time() - _arm) > 600.0:
                os.remove(_flag)
                return
        except Exception:
            pass
        # 按 device_id + arm 周期分文件：避免多腿帧混写(头 mode_code 被首写者锁定)，
        # 且每次重新 arm(touch) 都开一个新文件，杜绝同设备多通拼接（2026-09-11 事故根因）。
        _dev = (self.device_id or "unknown").replace("/", "_").replace("..", "_")
        _path = f"/opt/fuguoai-server/data/tmp/intercom_pair_{_dev}_{int(_arm)}.pcm"
        try:
            if os.path.exists(_path) and os.path.getsize(_path) > 8000000:
                return
            out_arr = np.frombuffer(out_pcm, dtype=np.int16)
            n = min(int(mic.size), int(ref.size), int(out_arr.size))
            if n <= 0:
                return
            inter = np.empty((n * 3,), dtype=np.int16)
            inter[0::3] = mic[:n].astype(np.int16)
            inter[1::3] = ref[:n].astype(np.int16)
            inter[2::3] = out_arr[:n]
            with open(_path, "ab") as _f:
                if os.path.getsize(_path) == 0:
                    _mode_code = 1 if self.mode == "2ch_srvref" else 2
                    _f.write(b"INTCPAIR")
                    _f.write(struct.pack("<IHH", self.sr, 3, _mode_code))
                _f.write(inter.tobytes())
        except Exception:
            pass

    def _auto_tune_step(self) -> None:
        """②③ 在线自适应（保守、可逆、默认关）。

        ② NLMS 寻优（auto_tune + multi_tap 时）：用 diag_summary 的 corr.weak_count
           占比判"残留非线性"（混响尾巴没消净）→ 增 tap_len + 降 nlms_mu（防发散）；
           corr.mean 高且稳 → 保持。带学习率与上下限钳制。
        ③ NER 自调（ner_auto + 2ch_srvref 时）：用 ref_active_rate 做 conservative 判断——
           ref_active_rate 过低=门控没 fire（ref 未喂入/配置未生效，属配置问题，不调 floor，
           避免误伤近端真声）；ref_active_rate 正常但 corr.mean 仍高=线性没消净且 NER 压不住
           → 略降 residual_floor 增强断环。跨层啸叫信号(watchdog teardown)本期不耦合，留 TODO。
        """
        if not self._diag_samples or len(self._diag_samples) < self._auto_step_min_samples:
            return
        try:
            s = self.diag_summary()
        except Exception:
            return
        if not s.get("diag"):
            return
        corr = s.get("corr", {})
        weak = int(corr.get("weak_count", 0))
        total = int(corr.get("strong_count", 0)) + weak
        corr_mean = float(corr.get("mean", 0.0) or 0.0)
        # ── ② NLMS 寻优 ──
        if self._auto_tune and self._multi_tap:
            weak_ratio = (weak / total) if total else 0.0
            # 残留非线性高 → 扩 FIR 抽头 + 降步长防发散；收敛好则不动
            if weak_ratio > 0.4 and corr_mean < 0.5:
                # 增 tap_len（扩窗覆盖更长混响尾巴）
                new_tap_ms = min(self._auto_tap_max_ms,
                                 (self._tap * 1000 // self.sr) * (1 + self._auto_lr))
                self._tap = max(64, int(new_tap_ms * self.sr // 1000))
                self._w = np.zeros(self._tap, dtype=np.float32)
                # 降 nlms_mu 防发散
                self._nlms_mu = max(self._auto_mu_min,
                                    self._nlms_mu * (1 - self._auto_lr))
            elif corr_mean >= 0.6 and weak_ratio < 0.1:
                # 收敛良好：tap_len 略收（省算力），μ 略升（加快跟踪），均不越界
                new_tap_ms = max(self._auto_tap_min_ms,
                                 (self._tap * 1000 // self.sr) * (1 - self._auto_lr * 0.5))
                self._tap = max(64, int(new_tap_ms * self.sr // 1000))
                self._nlms_mu = min(self._auto_mu_max,
                                    self._nlms_mu * (1 + self._auto_lr * 0.5))
        # ── ③ NER 自调 ──
        if self._ner_auto and self.mode == "2ch_srvref":
            ref_rate = s.get("ref_active_rate")
            if ref_rate is not None and 0.05 < ref_rate < 1.0:
                # 门控正常 fire；若线性仍没消净(corr_mean 高)且 NER 压不住 → 略降 floor 增强断环
                if corr_mean >= 0.6:
                    self._residual_floor = max(self._ner_floor_min,
                                               self._residual_floor * (1 - self._auto_lr))
            # ref_rate 过低（<0.05）或缺失：门控未 fire，属配置/ref 喂入问题，
            # 不自动调 floor（避免把近端真声也压没），交由 diag/运维排查。

    def _apply_residual_suppress(self, out_pcm: bytes, ref_active: bool,
                                 mic_rms: float) -> bytes:
        """非线性回声抑制(NER)兜底：以「远端是否在讲话(=设备扬声器此刻在播回声源)」为门控，
        固定衰减 mic 上行，断掉「扬声器→mic」正反馈环，保证环路增益恒<1，彻底杜绝啸叫。

        **为什么门控用「远端活跃」而非「mic↔ref 相关性」(上一版做法，5 轮仍啸叫的漏点)**：
        啸叫是正反馈环，啸叫音(窄带/失真/被放大)与语音 ref 相关性近 0 → 相关性门控
        对这些帧 echo_dom≈0 → 不衰减 → 环不断 → 仍啸叫。而回声源(90C0 扬声器)活跃的
        充要条件是「下行 ref(远端语音)在播」——只要远端在讲，设备扬声器就在放回声源，
        就必须衰减 mic 上行把环路增益压到 <1。这跟具体延迟/抖动无关(不要求对齐准确，
        靠「窗口内任一 ref 有能量」即可判活跃)，对 90C0 的 5~9s 秒级抖动完全免疫。

        衰减系数 = residual_floor(默认 0.3)：环路增益 = α(扬声器→mic 耦合, 0.3~0.7) × 0.3
        = 0.09~0.21 ≪1，稳断环。代价：远端讲话期间近端也被衰减(双讲略闷)，但远优于啸叫；
        远端静默(无回声源)不衰减，保近端。经典回声抑制器(非 AEC)做法，纯计算、可逆、不固化。
        """
        if not self._residual_suppress:
            return out_pcm
        if mic_rms < _MIC_RMS_FLOOR:
            return out_pcm
        if not ref_active:
            return out_pcm  # 远端静默、无回声源 → 不衰减，保近端真声
        atten = self._residual_floor
        out_f = np.frombuffer(out_pcm, dtype=np.int16).astype(np.float32)
        out_f = np.clip(out_f * atten, -32768, 32767)
        return out_f.astype(np.int16).tobytes()

    def _record_diag(self, delay_ms: int, corr: float):
        """把一帧的回声延迟估计与相关峰记入诊断样本（滑动窗口上限，防膨胀）。"""
        self._diag_collected = True
        self._diag_samples.append((delay_ms, corr))
        if len(self._diag_samples) > self._diag_max:
            # 丢弃最旧一半，保持滑动窗口
            self._diag_samples = self._diag_samples[self._diag_max // 2:]

    def _record_erle(self, mic_f: np.ndarray, ref_f: np.ndarray, out_pcm: bytes):
        """2ch 分支 ERLE 观测：mic_rms(消前) vs out_rms(消后) → 后端 AEC 实际抑制量。

        ERLE = 20*log10(mic_rms / out_rms) dB。判读：
          - ERLE ≥ 20dB：后端 AEC 抑制量足量（联调纪要要求的 20~25dB 补偿达成）；
          - ERLE ≈ 0~6dB：参考与回声不同源/对齐失效（同 2ch_srvref corr 恒 -1 的病）；
          - out_rms > mic_rms（ERLE<0）：谱减放大 → 必须立刻回退。
        ref_rms < _REF_RMS_FLOOR 的帧无回声源，不计入（避免静默帧稀释统计）。
        5s 节流打一条含滑窗中位，disable 时由 diag_summary 汇总。
        """
        mic_rms = float(np.sqrt(np.mean(mic_f ** 2)))
        ref_rms = float(np.sqrt(np.mean(ref_f ** 2)))
        if mic_rms < _MIC_RMS_FLOOR or ref_rms < _REF_RMS_FLOOR:
            return
        out_rms = float(np.sqrt(np.mean(
            np.frombuffer(out_pcm, dtype=np.int16).astype(np.float32) ** 2)))
        erle = 20.0 * np.log10(max(mic_rms, 1e-6) / max(out_rms, 1e-6))
        self._erle_samples.append((mic_rms, out_rms, float(erle)))
        if len(self._erle_samples) > self._diag_max // 2:
            self._erle_samples = self._erle_samples[len(self._erle_samples) // 2:]
        _now = time.time()
        if _now - self._erle_last_log >= 5.0:
            self._erle_last_log = _now
            _erles = sorted(e for _, _, e in self._erle_samples)
            _med = _erles[len(_erles) // 2] if _erles else float("nan")
            _logger.warning(
                f"[intercom][aec][erle] mode={self.mode} erle={erle:.1f}dB "
                f"median={_med:.1f}dB n={len(self._erle_samples)} "
                f"mic_rms={mic_rms:.0f} out_rms={out_rms:.0f} ref_rms={ref_rms:.0f}")

    def diag_summary(self) -> dict:
        """回声延迟实测统计：返回延迟分布与相关峰强度，判断回声路径真实延迟/方差/线性度。

        关键判读：
          - strong_delay_ms(相关峰强>=0.5 的样本)：这才是真实回声延迟的硬事实；
            若 min≈max(方差小) → 延迟近似恒定 → 服务端单抽头对消理论上可行；
            若 stdev 大(抖动) → 帧级对齐失效 → 啸叫根因，全双工不可行。
          - corr.strong_count vs weak_count：强相关多 → 回声是 ref 的线性副本(可消)；
            弱相关多 → 回声非线性/失真(单抽头难消) → 硬件/拓扑层面问题。
          - samples 为 0 / strong_count 为 0：说明该模式 mic 静音或无回声进入
            (如 outband 固件播放期静音) → 需改用 2ch_srvref 实测。
        """
        if not self._diag_samples:
            return {"diag": True, "samples": 0,
                    "note": "无样本(可能 mic 静音/无回声进入，或全程无相关峰>=0.3)"}
        import statistics
        delays = [d for d, _ in self._diag_samples]
        corrs = [c for _, c in self._diag_samples]
        strong = [d for d, c in self._diag_samples if c >= 0.5]
        res = {
            "diag": True,
            "samples": len(self._diag_samples),
            "delay_ms": {
                "min": min(delays), "max": max(delays),
                "mean": round(statistics.mean(delays), 1),
                "median": round(statistics.median(delays), 1),
                "stdev": round(statistics.pstdev(delays), 1) if len(delays) > 1 else 0.0,
            },
            "corr": {
                "mean": round(statistics.mean(corrs), 3),
                "min": round(min(corrs), 3),
                "max": round(max(corrs), 3),
                "strong_count": len(strong),
                "weak_count": len(delays) - len(strong),
            },
            "strong_delay_ms": (
                {"min": min(strong), "max": max(strong),
                 "mean": round(statistics.mean(strong), 1),
                 "median": round(statistics.median(strong), 1),
                 "stdev": round(statistics.pstdev(strong), 1) if len(strong) > 1 else 0.0}
                if strong else None),
            # NER 门控命中率：被衰减的帧占比。仅 2ch_srvref 有意义。
            # 远端持续讲话→接近 1.0(环路稳断)；接近 0→门控没 fire(可能 ref 未喂入/配置未生效)
            # → 仍啸叫的根因在门控本身而非延迟对齐。这是判环路稳定性的硬指标（corr 测不出）。
            "ref_active_rate": (round(self._ref_active_count / self._ref_active_total, 3)
                                if self._ref_active_total else None),
        }
        # ERLE 统计（2ch 分支才有样本）：后端 AEC 实际抑制量的硬数字，
        # 与设备侧 [HOWL-AGC] 增益爬升做双侧对照闭环。
        if self._erle_samples:
            import statistics as _st
            _erles = [e for _, _, e in self._erle_samples]
            res["erle"] = {
                "samples": len(_erles),
                "mean_db": round(_st.mean(_erles), 1),
                "median_db": round(_st.median(_erles), 1),
                "min_db": round(min(_erles), 1),
                "max_db": round(max(_erles), 1),
            }
        return res

    def _select_echo_ref_2ch(self, mic: np.ndarray, hist: list) -> np.ndarray:
        """从到达序 FIFO 历史里取当前 mic 帧的回声参考帧（int16 数组），无则 None。

        设备声学回声延迟实测恒定 1 帧(20ms，[R3-DELTA]=320 样本)：mic 帧 i 的回声 =
        播放参考帧 i-1(上一帧)的延迟副本。故回声源**恒为 hist[-2]**，直接取之，
        不做脆弱的频谱/波形相关搜索——真实连续语音相邻帧高度相似，相关搜索极易误选
        "同帧 ref"或"近端相关帧"→消不净→啸叫（单测已证该搜索劣于同帧）。

        帧序严格按到达序(FIFO)，hist[-2] 与 mic 帧 i 的回声严格对齐。上一帧 ref 静默
        (无播放)则无回声可消→None；偶发跳帧时小窗兜底到 4 帧前。
        """
        if mic.size == 0:
            return None
        # ⚠️ 必须 cast float32 再平方：int16**2 溢出 int16(32768² 不落) → 负值 → mean<0
        # → sqrt=NaN → NaN<floor 恒 False → 静默门槛被击穿（159 真机 RuntimeWarning 实证）
        if np.sqrt(np.mean(mic.astype(np.float32) ** 2)) < _MIC_RMS_FLOOR:
            return None
        if len(hist) < 2:
            return None
        best = hist[-2].astype(np.float32)  # 上一帧 ref = 回声源
        if np.sqrt(np.mean(best ** 2)) < _REF_RMS_FLOOR:
            # 上一帧静默：回声可能在更早非静默帧（设备偶发跳帧），小窗兜底到 4 帧前
            found = False
            for k in range(3, 5):  # hist[-4](3帧前) / hist[-5](4帧前)
                if len(hist) > k:
                    cand = hist[-k - 1].astype(np.float32)
                    if np.sqrt(np.mean(cand ** 2)) >= _REF_RMS_FLOOR:
                        best = cand
                        found = True
                        break
            if not found:
                return None
        return best

    def _maybe_capture_raw2ch(self, mic: np.ndarray, ref: np.ndarray):
        """诊断：把消回声前的原始 2ch 帧(双通道)落盘，用于生产实测 mic/ref 真实关系。

        由 flag 文件 /opt/.../data/tmp/.intercom_raw2ch 触发；无 flag 时零开销。
        文件 intercom_raw2ch.pcm：magic INTCRAW2 + sr/ch=2/frame + 2ch 交织 int16。
        上限 ~16s(600KB)，避免写爆。
        """
        _flag = "/opt/fuguoai-server/data/tmp/.intercom_raw2ch"
        if not os.path.exists(_flag):
            return
        _path = "/opt/fuguoai-server/data/tmp/intercom_raw2ch.pcm"
        try:
            if os.path.exists(_path) and os.path.getsize(_path) > 600000:
                return
            inter = np.empty((mic.size + ref.size,), dtype=np.int16)
            inter[0::2] = mic.astype(np.int16)
            inter[1::2] = ref.astype(np.int16)
            with open(_path, "ab") as _f:
                if os.path.getsize(_path) == 0:
                    _f.write(b"INTCRAW2")
                    _f.write(struct.pack("<IHH", self.sr, 2, 0))
                _f.write(inter.tobytes())
        except Exception:
            pass

    def process_opus(self, opus_frame: bytes, ref_cache: dict = None, mic_ts: int = None):
        """输入上行 opus 帧 → 消回声 → mono opus 帧；失败/无实例返回 None（调用方降级透传）。

        mode="2ch"    ：opus 为 2ch 交织，解交织后按到达序 FIFO 取"上一帧 ref"对齐谱减。
        mode="outband"/"2ch_srvref"：opus 解码为 mono → 与服务端自喂 ref 缓存互相关时延
            对齐谱减。上行 opus 帧时长由固件决定，未必是 20ms(320 样本)；若与 ref 子帧
            (320)不等长，_td_corr 因 len 不等恒返 -1.0 → AEC 完全失效。故 _process_mono_aec
            先把 mic 切成 320 样本(20ms)子帧，逐子帧对齐谱减再拼回，与 ref 归一一致。
        """
        try:
            pcm = self._dec.decode(bytes(opus_frame), _DECODE_MAX)
        except Exception:
            return None
        if not pcm or len(pcm) < 4:
            return None
        if mic_ts is None:
            mic_ts = int(time.time() * 1000)

        if self.mode == "2ch":
            mic, ref = self._deinterleave(pcm)
            if mic.size == 0:
                return None
            # 诊断：消回声前的原始双通道帧落盘（arm .intercom_raw2ch flag 才生效，零开销）
            self._maybe_capture_raw2ch(mic, ref)
            # 按到达序把本帧 ch2(ref)压入 FIFO；回声源恒为上一帧 ref(hist[-2])。
            # 不依赖墙钟 ts（真机突发发帧会致 ts 键碰撞覆盖历史）→ 用 FIFO 免疫。
            self._ref_hist.append(ref.astype(np.int16))
            if len(self._ref_hist) > self._ref_hist_max:
                self._ref_hist.pop(0)
            echo_ref = self._select_echo_ref_2ch(mic, self._ref_hist)
            if echo_ref is None:
                out_pcm = mic.tobytes()  # 静默/历史不足 → 原样返回，零损伤降级
            else:
                out_pcm = self._spectral_subtract(
                    mic, echo_ref, scale_cap=self._echo_gain_cap)
                # 2ch 分支观测补齐：参考(ch1)与回声同源，delay 恒为 1 帧帧长；
                # corr=mic 与 echo_ref 的互相关（对齐质量），ERLE=实际抑制量。
                if self._diag:
                    _corr = self._td_corr(mic.astype(np.float32), echo_ref)
                    self._record_diag(int(mic.size / self.sr * 1000), _corr)
                self._record_erle(mic.astype(np.float32), echo_ref, out_pcm)
            # ── NER(下行能量门控)兜底 ──
            # 8818 ch1 实测未馈入播放 PCM(raw2ch 铁证 2026-09-06: 中位 RMS=4、60-73Hz 杂散、
            # corr 0.129) → 线性对消无米下锅 → 啸叫环不破、设备端 AGC 被钳死不可闻。
            # 以服务端下行(ref_cache 近 300ms 窗)能量判「远端正在讲话」：是则把设备上行
            # 按 residual_floor 衰减——环路增益硬压 <1 断环，AGC 松开恢复可闻；近端说话
            # (下行静默)不衰减保真声。voicebox 修好 ch1 后谱减自动接管，NER 退居保险。
            if self._residual_suppress:
                _ref_active = False
                for _ts, _pcm in (ref_cache or {}).items():
                    if mic_ts - 300 <= _ts <= mic_ts:
                        _r = np.frombuffer(_pcm, dtype=np.int16).astype(np.float32)
                        if _r.size and float(np.sqrt(np.mean(_r ** 2))) >= _REF_RMS_FLOOR:
                            _ref_active = True
                            break
                if _ref_active:
                    self._ref_active_hold_until = mic_ts + self._ref_active_hold_ms
                _ref_active_eff = _ref_active or (mic_ts < self._ref_active_hold_until)
                self._ref_active_total += 1
                if _ref_active_eff:
                    self._ref_active_count += 1
                _mic_rms = float(np.sqrt(np.mean(mic.astype(np.float32) ** 2)))
                out_pcm = self._apply_residual_suppress(out_pcm, _ref_active_eff, _mic_rms)
        else:
            # outband / 2ch_srvref：mono mic → 服务端自喂 ref 缓存互相关时延对齐谱减。
            # 2ch_srvref 设备 B2 全双工(2ch 上行、mic 不静音)，但本机固件 ch1(ref) 实测
            # 为空/坏→丢弃坏 ch1，mic 取 ch0，回声参考改走服务端自喂(下行播放音频)。
            mic_all = np.frombuffer(pcm, dtype=np.int16)
            if self.mode == "2ch_srvref":
                # 临时诊断（2026-09-10）：原始 ch0/ch1 落盘——判定哪一路是真 mic。
                # flag .intercom_raw2ch 触发（与 2ch 模式 raw2ch 共用 flag）；上限 600KB。
                try:
                    if os.path.exists("/opt/fuguoai-server/data/tmp/.intercom_raw2ch"):
                        _r2p = "/opt/fuguoai-server/data/tmp/intercom_raw2ch_srvref.pcm"
                        if (not os.path.exists(_r2p)) or os.path.getsize(_r2p) < 8000000:
                            _c0 = mic_all[0::2]
                            _c1 = mic_all[1::2]
                            _m = min(_c0.size, _c1.size)
                            _inter = np.empty((_m * 2,), dtype=np.int16)
                            _inter[0::2] = _c0[:_m].astype(np.int16)
                            _inter[1::2] = _c1[:_m].astype(np.int16)
                            with open(_r2p, "ab") as _f:
                                if os.path.getsize(_r2p) == 0:
                                    _f.write(b"INTCRAW3")
                                    _f.write(struct.pack("<IHH", self.sr, 2, 0))
                                _f.write(_inter.tobytes())
                except Exception:
                    pass
                # 临时诊断（2026-09-10 18:30）：服务端自喂播放参考(ref_cache)落盘。
                # 目的：离线判定设备上行 ch1 是否就是「设备扬声器播放信号」——与 ref 高相关
                # = ch1 可做 AEC 参考(可复活 2ch 模式)；与 ref 不相关 = ch1 是别的东西。
                # flag .intercom_refdump 触发；同一 mic_ts 只写一次；上限 8MB。
                try:
                    if os.path.exists("/opt/fuguoai-server/data/tmp/.intercom_refdump"):
                        _rdp = "/opt/fuguoai-server/data/tmp/intercom_refdump.pcm"
                        _done = getattr(self, "_ref_dumped_ts", None)
                        if _done is None:
                            _done = set()
                            self._ref_dumped_ts = _done
                        if (not os.path.exists(_rdp)) or os.path.getsize(_rdp) < 8000000:
                            for _rts, _rpcm in sorted((ref_cache or {}).items()):
                                if _rts in _done:
                                    continue
                                _done.add(_rts)
                                with open(_rdp, "ab") as _f:
                                    if os.path.getsize(_rdp) == 0:
                                        _f.write(b"INTCREF1")
                                        _f.write(struct.pack("<IHH", self.sr, 1, 0))
                                    _a = np.frombuffer(_rpcm, dtype=np.int16)
                                    _f.write(struct.pack("<Q", int(_rts)))
                                    _f.write(struct.pack("<I", int(_a.size)))
                                    _f.write(_a.astype(np.int16).tobytes())
                except Exception:
                    pass
                # 临时诊断（2026-09-08）：ch0/ch1 分通道电平——判设备上行两通道各装了
                # 什么(ch0=mic 假设是否成立)、设备端是否在扬声器出声时把 mic 压到静音
                # (voicebox AGC/回声抑制钳死嫌疑)。节流 5s，稳定后删除本段。
                _now = time.time()
                if _now - getattr(self, "_ch_lvl_last", 0.0) >= 5.0:
                    self._ch_lvl_last = _now
                    _ch0 = mic_all[0::2].astype(np.float32)
                    _ch1 = mic_all[1::2].astype(np.float32)
                    _logger.warning(
                        f"[intercom][aec][chlvl] ch0_rms="
                        f"{float(np.sqrt(np.mean(_ch0 ** 2))):.0f} "
                        f"ch1_rms={float(np.sqrt(np.mean(_ch1 ** 2))):.0f} "
                        f"ch0_peak={int(np.max(np.abs(_ch0))) if _ch0.size else 0} "
                        f"ch1_peak={int(np.max(np.abs(_ch1))) if _ch1.size else 0}")
                # A/B 实验（2026-09-10 18:22）：mic 通道可选——静音测试显示 ch1 才是设备麦克风。
                # flag `.intercom_mic_ch1` 存在则取 ch1 作 mic，否则取 ch0（原行为）。
                # 改 flag 即时生效（每帧读），无需重启；用于判定固件 ch0/ch1 是否与后端假设相反。
                _mic_src = 1 if os.path.exists("/opt/fuguoai-server/data/tmp/.intercom_mic_ch1") else 0
                mic_all = mic_all[_mic_src::2]   # 原固定 ch0(mic)+丢弃 ch1(ref)
            # 一次性诊断：确认上行 opus 帧真实时长(样本数)，便于判读 corr 分布。
            # 由 flag 文件 /opt/fuguoai-server/data/tmp/.intercom_diag_len 触发(零开销默认关)。
            _diag_len_flag = "/opt/fuguoai-server/data/tmp/.intercom_diag_len"
            if os.path.exists(_diag_len_flag) and \
                    not getattr(self, "_diag_len_logged", False):
                self._diag_len_logged = True
                _logger.warning(
                    f"[intercom][aec][diag_len] mode={self.mode} "
                    f"uplink_pcm_samples={mic_all.size} "
                    f"(={mic_all.size / self.sr * 1000:.0f}ms) "
                    f"ref_subframe_samples={FRAME_SAMPLES}")
            out_pcm = self._process_mono_aec(mic_all, ref_cache or {}, mic_ts)

        try:
            return self._enc.encode(bytes(out_pcm), len(out_pcm) // 2)
        except Exception:
            return None

    def _process_mono_aec(self, mic_all: np.ndarray, ref_cache: dict, mic_ts: int) -> bytes:
        """mono mic(任意上行帧时长) → 按 320 样本(20ms)子帧归一 → 逐子帧 process_ref_cache
        → 拼接回完整 PCM。

        上行 opus 帧时长由固件决定(20/40/60ms...)，解码后 mic 可能 320/640/1920 样本；
        而 ref 子帧恒为 320。若整帧直接送 process_ref_cache，len(mic)!=len(ref) → 互相关
        恒 -1.0 → AEC 失效(2ch_srvref 真机 corr 全 -1.0、delay 钉死 max、啸叫的根因)。
        切子帧后 mic 子帧与 ref 子帧等长，互相关正常工作；当 mic 本就 320 时退化为单帧
        调用(与旧行为一致，零回归)。
        """
        n = mic_all.size
        if n == 0:
            return b""
        if n <= FRAME_SAMPLES:
            return self.process_ref_cache(mic_all.astype(np.int16).tobytes(), ref_cache, mic_ts)
        n_sub = n // FRAME_SAMPLES
        # 子帧 ts：整帧在 mic_ts 收到，子帧 i 为其第 i 段，捕获时刻早于 mic_ts，
        # 按 20ms 递减(最老子帧=mic_ts-(n_sub-1)*20)，与 ref 子帧 ts 同基准对齐。
        out_chunks = []
        for i in range(n_sub):
            sub = mic_all[i * FRAME_SAMPLES:(i + 1) * FRAME_SAMPLES]
            sub_ts = mic_ts - (n_sub - 1 - i) * FRAME_MS
            sub_out = self.process_ref_cache(sub.astype(np.int16).tobytes(), ref_cache, sub_ts)
            out_chunks.append(np.frombuffer(sub_out, dtype=np.int16))
        rem = n - n_sub * FRAME_SAMPLES
        if rem > 0:
            out_chunks.append(mic_all[n_sub * FRAME_SAMPLES:])  # 余数(<320)原样保留
        if not out_chunks:
            return mic_all.astype(np.int16).tobytes()
        return np.concatenate(out_chunks).astype(np.int16).tobytes()
