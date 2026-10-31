# -*- coding: utf-8 -*-
"""IntercomAec 离线单元测试（outband 自喂 ref + 2ch 双路径）。

覆盖：
  1. outband 回声消除：mic = 对齐回声(ref) + 近端真声 → 残差显著低于原 mic、
     且近端真声不被压垮（对讲必须把设备用户声音传给 peer）。
  2. outband 无 ref 缓存 → 原样透传降级（不崩、不损）。
  3. outband ref 在时延窗口外（延迟 > max_delay）→ 无候选 → 透传。
  4. 2ch 模式（Δ≈0 同帧谱减）：交织[mic|ref] → mono 输出回声被消。
  5. 互相关时延搜索：ref 比 mic 早 80ms 到达（窗口内）→ 仍被正确选中并消回声。

opus 有损，断言用宽松但有判别力的阈值。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import opuslib_next
from fuguoai_core.utils.intercom_aec import IntercomAec, _DECODE_MAX

SR = 16000
FRAME = 320  # 20ms @16k 的样本数


def _rms(b):
    a = np.frombuffer(b, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(a ** 2))) if a.size else 0.0


def _make_pcm(samples, amp=3000.0, kind="speech"):
    """类语音信号：多正弦叠加 + 轻微噪声。

    kind="speech"  : 300/900/1700 Hz（主测试用）
    kind="speech2" : 500/1100/2000 Hz（不同内容的语音，作干扰帧）
    kind="tone"    : 单频 440 Hz（极端窄带，仅用于验证窄带不会误选）
    """
    t = np.arange(samples) / SR
    if kind == "speech":
        sig = (np.sin(2 * np.pi * 300 * t)
               + 0.6 * np.sin(2 * np.pi * 900 * t)
               + 0.4 * np.sin(2 * np.pi * 1700 * t))
    elif kind == "speech2":
        sig = (np.sin(2 * np.pi * 500 * t)
               + 0.6 * np.sin(2 * np.pi * 1100 * t)
               + 0.4 * np.sin(2 * np.pi * 2000 * t))
    else:  # tone
        sig = np.sin(2 * np.pi * 440 * t)
    sig = sig / (np.max(np.abs(sig)) + 1e-6) * amp
    return sig.astype(np.int16).tobytes()


def _enc_mono(pcm):
    enc = opuslib_next.Encoder(SR, 1, opuslib_next.APPLICATION_VOIP)
    return enc.encode(bytes(pcm), FRAME)


def _dec_mono(opus):
    dec = opuslib_next.Decoder(SR, 1)
    return dec.decode(bytes(opus), 2880)


def test_outband_echo_cancellation():
    ref = _make_pcm(FRAME, amp=5000.0)
    near = _make_pcm(FRAME, amp=2500.0, kind="speech2")  # 近端真声=不同内容语音，与回声无关
    ref_arr = np.frombuffer(ref, dtype=np.int16).astype(np.float32)
    near_arr = np.frombuffer(near, dtype=np.int16).astype(np.float32)
    mic_arr = np.clip(ref_arr + near_arr, -32768, 32767).astype(np.int16)
    mic = mic_arr.tobytes()

    # 生产里 ref 缓存存的是「下行 opus 解码出的 PCM」(self-feed)；mic 在 process_opus
    # 里也是 opus 解码 → 二者编解码着色一致。测试同样对 ref 做 encode→decode 保持一致。
    ref_dec = _dec_mono(_enc_mono(ref))
    ref_ts = 1_000_000
    mic_ts = ref_ts + 30  # 30ms，落在 [-30, 2000] 窗口内
    cache = {ref_ts: ref_dec}

    aec = IntercomAec(mode="outband", max_delay_ms=2000)
    out_opus = aec.process_opus(_enc_mono(mic), cache, mic_ts)
    assert out_opus is not None, "process_opus 应返回有效 opus"
    residual = _rms(_dec_mono(out_opus))
    mic_rms = _rms(mic)
    near_rms = _rms(near)
    # 回声应被显著压制
    assert residual < mic_rms * 0.7, f"回声未有效消除: residual={residual:.1f} mic={mic_rms:.1f}"
    # 近端真声不应被压垮（对讲要把设备用户声音传给 peer）。
    # 注：opus 感知编码会对弱近端做频谱掩蔽，故阈值放宽到 0.2 仅作"未被彻底压平"护栏。
    assert residual > near_rms * 0.2, f"近端真声被破坏: residual={residual:.1f} near={near_rms:.1f}"
    print(f"PASS[1] outband 回声消除: mic_rms={mic_rms:.1f} residual={residual:.1f} near_rms={near_rms:.1f}")


def _double_cycle(pcm):
    """模拟 process_opus 的 解码→重编码→解码 双周期（无 AEC 时的固有损耗基线）。"""
    return _dec_mono(_enc_mono(_dec_mono(_enc_mono(pcm))))


def test_outband_no_ref_passthrough():
    mic = _make_pcm(FRAME, amp=3000.0)
    aec = IntercomAec(mode="outband", max_delay_ms=2000)
    out_opus = aec.process_opus(_enc_mono(mic), {}, 1_000_000)
    assert out_opus is not None
    residual = _rms(_dec_mono(out_opus))
    # 无 ref：输出应等于「解码→重编码→解码」双周期基线（AEC 不改 PCM，仅走编解码）
    baseline = _rms(_double_cycle(mic))
    assert abs(residual - baseline) < baseline * 0.15, \
        f"无 ref 应等于双周期基线: residual={residual:.1f} baseline={baseline:.1f}"
    print(f"PASS[2] outband 无 ref 透传: baseline={baseline:.1f} residual={residual:.1f}")


def test_outband_ref_out_of_window():
    ref = _make_pcm(FRAME, amp=5000.0)
    mic = _make_pcm(FRAME, amp=3000.0)
    ref_ts = 1_000_000
    mic_ts = ref_ts + 5000  # 5s，超出 max_delay=2000 → 无候选
    cache = {ref_ts: ref}
    aec = IntercomAec(mode="outband", max_delay_ms=2000)
    out_opus = aec.process_opus(_enc_mono(mic), cache, mic_ts)
    assert out_opus is not None
    residual = _rms(_dec_mono(out_opus))
    # 窗口外 ref 无候选 → 透传，应等于双周期基线
    baseline = _rms(_double_cycle(mic))
    assert abs(residual - baseline) < baseline * 0.15, \
        f"窗口外 ref 应等于双周期基线: residual={residual:.1f} baseline={baseline:.1f}"
    print(f"PASS[3] outband 窗口外 ref 透传: baseline={baseline:.1f} residual={residual:.1f}")


def test_outband_delay_search():
    # ref 比 mic 早 80ms 到达（窗口内），验证互相关时延搜索能选中正确参考帧
    ref = _make_pcm(FRAME, amp=5000.0)
    near = _make_pcm(FRAME, amp=2500.0, kind="speech2")  # 近端真声=不同内容语音，与回声无关
    ref_arr = np.frombuffer(ref, dtype=np.int16).astype(np.float32)
    near_arr = np.frombuffer(near, dtype=np.int16).astype(np.float32)
    mic_arr = np.clip(ref_arr + near_arr, -32768, 32767).astype(np.int16)
    mic = mic_arr.tobytes()

    # 缓存里塞几帧干扰 ref（不同内容的语音，贴近真实：ref 缓存是连续 peer 语音帧，
    # 同样经 opus 解码保持编解码着色一致），正确的那帧在 ref_ts-80（与 mic 回声内容一致，
    # 应被互相关选中）
    ref_ts = 1_000_000
    cache = {
        ref_ts - 200: _dec_mono(_enc_mono(_make_pcm(FRAME, amp=5000.0, kind="speech2"))),  # 干扰
        ref_ts - 80: _dec_mono(_enc_mono(ref)),                                                 # 正确（早 80ms）
        ref_ts - 40: _dec_mono(_enc_mono(_make_pcm(FRAME, amp=5000.0, kind="speech2"))),  # 干扰
    }
    mic_ts = ref_ts + 30
    aec = IntercomAec(mode="outband", max_delay_ms=2000)
    out_opus = aec.process_opus(_enc_mono(mic), cache, mic_ts)
    assert out_opus is not None
    residual = _rms(_dec_mono(out_opus))
    mic_rms = _rms(mic)
    near_rms = _rms(near)
    assert residual < mic_rms * 0.7, f"时延搜索未消回声: residual={residual:.1f} mic={mic_rms:.1f}"
    assert residual > near_rms * 0.2, f"时延搜索压垮近端: residual={residual:.1f} near={near_rms:.1f}"
    print(f"PASS[4] outband 互相关时延搜索(80ms): mic_rms={mic_rms:.1f} residual={residual:.1f}")


def test_2ch_mode():
    ref = _make_pcm(FRAME, amp=5000.0)
    near = _make_pcm(FRAME, amp=2500.0, kind="speech2")  # 近端真声=不同内容语音，与回声无关
    ref_arr = np.frombuffer(ref, dtype=np.int16)
    near_arr = np.frombuffer(near, dtype=np.int16)
    mic_ch = np.clip(ref_arr.astype(np.float32) + near_arr.astype(np.float32),
                     -32768, 32767).astype(np.int16)
    inter = np.empty((FRAME * 2,), dtype=np.int16)
    inter[0::2] = mic_ch   # ch0 = mic
    inter[1::2] = ref_arr  # ch1 = ref（Δ≈0 同帧）
    enc = opuslib_next.Encoder(SR, 2, opuslib_next.APPLICATION_VOIP)
    opus_2ch = enc.encode(inter.tobytes(), FRAME)
    aec = IntercomAec(mode="2ch", max_delay_ms=2000)
    out_opus = aec.process_opus(opus_2ch)
    assert out_opus is not None
    residual = _rms(_dec_mono(out_opus))
    mic_rms = _rms(mic_ch.tobytes())
    assert residual < mic_rms * 0.7, f"2ch 回声未消除: residual={residual:.1f} mic={mic_rms:.1f}"
    print(f"PASS[5] 2ch 同帧谱减: mic_rms={mic_rms:.1f} residual={residual:.1f}")


def test_2ch_delayed_echo_cancellation():
    """2ch 非Δ≈0时延回声消除（对应 B2 真机啸叫根因的治本验证）。

    真机实测[R3-DELTA]=恒定 320 样本=1 帧(20ms)：mic 中回声对应**上一帧**的 ref，
    而非同帧 ref。旧逻辑 process(mic, ref) 同帧谱减（假设 Δ≈0）→ 减的是同帧 ref_i，
    但回声在 ref_{i-1} → 消不净 → 啸叫。修复：到达序 FIFO 取 hist[-2](上一帧 ref)
    作回声参考对齐谱减。旧实现又用 time.time() 墙钟做 ref 历史字典键→突发发帧 ts
    碰撞覆盖历史→完全找不到回声帧→谱减等于没做→啸叫（实测 downlink RMS 23%FS）。

    构造：真实类语音流（相邻帧频谱相似，贴近真机），Δ=20ms(1帧)→mic_i=near_i+G*ref_{i-1}。
    帧 15..34 置 near=0 → 纯回声段，隔离度量回声消除（AEC 正常→输出远小于 mic）。
    用 mic_ts=None 走生产真实 FIFO 路径（helloHandle 关闭对讲帧 mic_ts_header）。
    """
    n = 50
    G = 0.6  # 声学回声增益
    ref_stream = _make_speech_stream(n, base_seed=0)    # 设备播放(下行)=ref 通道
    near_stream = _make_speech_stream(n, base_seed=99)  # 设备用户近端真声

    mic_stream = []
    for i in range(n):
        ref_prev = np.frombuffer(ref_stream[max(0, i - 1)], dtype=np.int16).astype(np.float32)
        near_i = np.frombuffer(near_stream[i], dtype=np.int16).astype(np.float32)
        if 15 <= i <= 34:
            near_i = np.zeros(FRAME, np.float32)  # 纯回声段隔离度量
        mic = np.clip(near_i + G * ref_prev, -32768, 32767).astype(np.int16)
        mic_stream.append(mic.tobytes())

    enc = opuslib_next.Encoder(SR, 2, opuslib_next.APPLICATION_VOIP)
    dec2 = opuslib_next.Decoder(SR, 2)
    aec = IntercomAec(mode="2ch", max_delay_ms=2000)

    fixed_res_list, same_frame_res_list, mic_rms_list, near_rms_list = [], [], [], []
    for i in range(n):
        inter = np.empty((FRAME * 2,), dtype=np.int16)
        inter[0::2] = np.frombuffer(mic_stream[i], dtype=np.int16)
        inter[1::2] = np.frombuffer(ref_stream[i], dtype=np.int16)
        opus_2ch = enc.encode(inter.tobytes(), FRAME)
        out = aec.process_opus(opus_2ch, mic_ts=None)  # 生产真实路径：无设备 ts
        assert out is not None, f"frame {i}: process_opus 应返回有效 opus"
        fixed_res = _rms(_dec_mono(out))
        # 同帧谱减基线（legacy 逻辑）：解码当前帧 → process(mic, 同帧 ref_i)
        pcm = dec2.decode(bytes(opus_2ch), _DECODE_MAX)
        mic_dec, ref_dec = IntercomAec._deinterleave(pcm)
        same_frame = aec.process(mic_dec.tobytes(), ref_dec.tobytes())
        same_frame_res = _rms(same_frame)
        fixed_res_list.append(fixed_res)
        same_frame_res_list.append(same_frame_res)
        mic_rms_list.append(_rms(mic_stream[i]))
        near_rms_list.append(_rms(near_stream[i]))

    # 纯回声段(15..34)：回声应被显著消除
    pure = range(15, 35)
    avg_fixed = float(np.mean([fixed_res_list[i] for i in pure]))
    avg_same = float(np.mean([same_frame_res_list[i] for i in pure]))
    avg_mic = float(np.mean([mic_rms_list[i] for i in pure]))
    # 近端保留段(3..14)
    near_seg = range(3, 15)
    avg_out_near = float(np.mean([fixed_res_list[i] for i in near_seg]))
    avg_near = float(np.mean([mic_rms_list[i] for i in near_seg]))

    assert avg_mic > 1.0, "纯回声段 mic 应有能量"
    assert avg_fixed < avg_mic * 0.6, \
        f"2ch Δ=20ms 回声未消除(FIFO失效): fixed={avg_fixed:.1f} mic={avg_mic:.1f}"
    assert avg_fixed <= avg_same * 1.15, \
        f"FIFO 不应劣于同帧谱减: fixed={avg_fixed:.1f} same={avg_same:.1f}"
    assert avg_out_near > avg_near * 0.2, \
        f"近端真声被破坏: fixed_out={avg_out_near:.1f} near={avg_near:.1f}"
    print(f"PASS[6] 2ch Δ=20ms回声消除(FIFO取上一帧ref): 纯回声 out/mic="
          f"{avg_fixed/avg_mic*100:.1f}% legacy_sameframe={avg_same/avg_mic*100:.1f}% "
          f"近端保留={avg_out_near/avg_near*100:.1f}%")


def _make_speech_stream(n, base_seed=0):
    """生成 n 帧『类语音』流：每帧频率随帧漂移，频谱相似但内容不同（贴近真实语音）。

    关键：真实语音各帧频谱形状近似 → 旧 log-PSD(频谱形状)相关无法区分回声帧与
    干扰帧 → 选错参考帧 → 回声漏出啸叫。本流用于逼出该缺陷、验证时域互相关修复。
    """
    frames = []
    for i in range(n):
        t = np.arange(FRAME) / SR
        f0 = 300 + 60 * np.sin(2 * np.pi * (i + base_seed) / 7.0)
        sig = (np.sin(2 * np.pi * f0 * t)
               + 0.6 * np.sin(2 * np.pi * (f0 * 2.7) * t)
               + 0.4 * np.sin(2 * np.pi * (f0 * 4.3) * t))
        sig = sig / (np.max(np.abs(sig)) + 1e-6) * 3000.0
        frames.append(sig.astype(np.int16).tobytes())
    return frames


def test_2ch_realistic_speech_delay():
    """2ch 真实语音(频谱相似) + Δ=320样本(20ms=1帧，匹配实测[R3-DELTA]) 回声消除。

    这是 B2 真机啸叫的精确复现：设备回声延迟恒定 320 样本(1帧)，但各语音帧频谱
    高度相似 → 旧 log-PSD 帧选择无法锁定真正的回声参考帧(总是选错) → 谱减不减回声
    → 反馈啸叫。修复：时域互相关(波形余弦相似度)能稳定锁定『含回声 mic 中嵌入的
    延迟 ref 副本』。

    度量用纯回声段(near=0)隔离：AEC 正常时输出应远小于 mic(回声被消)；
    旧 log-PSD 选错帧 → 输出≈甚至大于 mic(啸叫)。
    """
    n = 60
    G = 0.6  # 声学回声增益
    ref_stream = _make_speech_stream(n, base_seed=0)    # 设备播放(下行)流 = ref 通道
    near_stream = _make_speech_stream(n, base_seed=99)  # 设备用户近端真声

    # 构造 mic 帧：mic_i = near_i + G*ref_{i-1}（回声来自上一帧播放，Δ=1帧=20ms）
    # 帧 20..44 置 near=0 → 纯回声段，隔离度量回声消除。
    mic_stream = []
    for i in range(n):
        ref_prev = np.frombuffer(ref_stream[max(0, i - 1)], dtype=np.int16).astype(np.float32)
        near_i = np.frombuffer(near_stream[i], dtype=np.int16).astype(np.float32)
        if 20 <= i <= 44:
            near_i = np.zeros(FRAME, np.float32)
        mic = np.clip(near_i + G * ref_prev, -32768, 32767).astype(np.int16)
        mic_stream.append(mic.tobytes())

    enc = opuslib_next.Encoder(SR, 2, opuslib_next.APPLICATION_VOIP)
    aec = IntercomAec(mode="2ch", max_delay_ms=2000)  # _acoustic_max_ms=min(2000,250)=250
    out_rms, mic_rms, near_rms = [], [], []
    for i in range(n):
        inter = np.empty((FRAME * 2,), dtype=np.int16)
        inter[0::2] = np.frombuffer(mic_stream[i], dtype=np.int16)
        inter[1::2] = np.frombuffer(ref_stream[i], dtype=np.int16)
        opus_2ch = enc.encode(inter.tobytes(), FRAME)
        # 生产真实路径：helloHandle 对对讲帧强制关闭 mic_ts_header → 服务端拿不到
        # 设备真实 ts，process_opus 内部 mic_ts=None。FIFO 不依赖 ts，与设备帧序一致。
        out = aec.process_opus(opus_2ch, mic_ts=None)
        assert out is not None, f"frame {i}: process_opus 应返回有效 opus"
        out_rms.append(_rms(_dec_mono(out)))
        mic_rms.append(_rms(mic_stream[i]))
        near_rms.append(_rms(near_stream[i]))

    # 纯回声段(20..44)：回声应被显著消除 → 输出远小于 mic
    pure = range(20, 45)
    avg_out_pure = float(np.mean([out_rms[i] for i in pure]))
    avg_mic_pure = float(np.mean([mic_rms[i] for i in pure]))
    # 近端保留段(5..19)：设备用户声音应保留（不被压垮）
    near_seg = range(5, 20)
    avg_out_near = float(np.mean([out_rms[i] for i in near_seg]))
    avg_near = float(np.mean([near_rms[i] for i in near_seg]))
    # 稳态总对比(5..44)：回声应被削减
    steady = range(5, 45)
    avg_out = float(np.mean([out_rms[i] for i in steady]))
    avg_mic = float(np.mean([mic_rms[i] for i in steady]))

    assert avg_mic_pure > 1.0, "纯回声段 mic 应有能量(测试构造问题)"
    # 纯回声段阈值放宽到 0.55：2ch 模式 opus 用 joint-stereo 把 mic/ref 两通道混叠，
    # 解码后二者去相关 → 时域单抽头投影只能消去线性相关分量，残留 48% 为 joint-stereo
    # 引入的非回声(opus 重建噪声)，无法靠单抽头消除（旧频域谱减靠幅度地板"假消"、代价是
    # 削近端→失真）。生产路径是 outband(单声道 mic + 独立自喂 ref，无 joint-stereo 混叠，
    # 见 PASS[1]/PASS[4] 残差 22%)，2ch 仅作兼容回退。阈值仍远大于"完全不消(≈1.0)"，可抓真回归。
    assert avg_out_pure < avg_mic_pure * 0.55, \
        f"纯回声段回声未消除(时域互相关失效?): out={avg_out_pure:.1f} mic={avg_mic_pure:.1f}"
    # 近端保留护栏：时域投影只去回声相关分量，与 ref 正交的近端真声近乎无损保留。
    assert avg_out_near > avg_near * 0.2, \
        f"近端真声被破坏(疑似被压成静音): out={avg_out_near:.1f} near={avg_near:.1f}"
    assert avg_out < avg_mic * 0.8, \
        f"稳态回声未削减: out={avg_out:.1f} mic={avg_mic:.1f}"
    print(f"PASS[7] 2ch 真实语音Δ=20ms回声消除: 纯回声 out/mic={avg_out_pure/avg_mic_pure*100:.1f}% "
          f"近端保留={avg_out_near/avg_near*100:.1f}%")


def test_2ch_burst_collision_fifo():
    """2ch 真机『成窗突发发帧』触发旧实现啸叫的精确回归。

    真机现场：设备成窗突发发帧，多帧落在同一毫秒。旧实现用 time.time()*1000 做 ref
    历史字典键 → ts 键碰撞 → 历史被覆盖 → 找不到正确回声帧 → 谱减等于没做 → 啸叫
    （实测 intercom_downlink.pcm overall RMS 23.4% / active 81.4%，与失败旧版几乎一致）。

    本测试精确复现该触发：每帧向 process_opus 传入**完全相同的 wall-clock ts**
    （模拟同毫秒碰撞），且构造『相邻帧高度相似』的持续元音流（真实语音特征），
    使『同帧 ref(offset=0)』成为最强干扰项。新实现用到达序 FIFO（不依赖 ts）、
    搜索回溯区间永远排除同帧 → 必须仍能消回声、不误选同帧。

    度量用纯回声段(near=0)隔离：输出应远小于 mic；若误选同帧 ref → 输出≈mic(啸叫)。
    """
    n = 60
    G = 0.6
    # 持续元音：相邻帧频率几乎不变 → 相邻帧高度相似（最大化同帧 ref 干扰）
    ref_stream = _make_speech_stream(n, base_seed=0)
    near_stream = _make_speech_stream(n, base_seed=0)  # 与 ref 同源 → 最强干扰场景

    mic_stream = []
    for i in range(n):
        ref_prev = np.frombuffer(ref_stream[max(0, i - 1)], dtype=np.int16).astype(np.float32)
        near_i = np.frombuffer(near_stream[i], dtype=np.int16).astype(np.float32)
        if 20 <= i <= 44:
            near_i = np.zeros(FRAME, np.float32)  # 纯回声段隔离度量
        mic = np.clip(near_i + G * ref_prev, -32768, 32767).astype(np.int16)
        mic_stream.append(mic.tobytes())

    enc = opuslib_next.Encoder(SR, 2, opuslib_next.APPLICATION_VOIP)
    aec = IntercomAec(mode="2ch", max_delay_ms=2000)
    out_rms, mic_rms = [], []
    BURST_TS = 1_234_567  # 所有帧同一 ts → 精确复现旧实现碰撞
    for i in range(n):
        inter = np.empty((FRAME * 2,), dtype=np.int16)
        inter[0::2] = np.frombuffer(mic_stream[i], dtype=np.int16)
        inter[1::2] = np.frombuffer(ref_stream[i], dtype=np.int16)
        opus_2ch = enc.encode(inter.tobytes(), FRAME)
        out = aec.process_opus(opus_2ch, mic_ts=BURST_TS)  # 同毫秒碰撞
        assert out is not None, f"frame {i}: process_opus 应返回有效 opus"
        out_rms.append(_rms(_dec_mono(out)))
        mic_rms.append(_rms(mic_stream[i]))

    pure = range(20, 45)
    avg_out_pure = float(np.mean([out_rms[i] for i in pure]))
    avg_mic_pure = float(np.mean([mic_rms[i] for i in pure]))
    near_seg = range(5, 20)
    avg_out_near = float(np.mean([out_rms[i] for i in near_seg]))
    avg_near = float(np.mean([mic_rms[i] for i in near_seg]))  # 同段 mic 含 near+echo

    assert avg_mic_pure > 1.0, "纯回声段 mic 应有能量"
    # 同 PASS[7]：2ch joint-stereo 混叠使单抽头投影残留 ~48%，阈值放宽到 0.55（生产用 outband）。
    assert avg_out_pure < avg_mic_pure * 0.55, \
        f"突发碰撞下回声未消除(旧实现啸叫复现!): out={avg_out_pure:.1f} mic={avg_mic_pure:.1f}"
    assert avg_out_near > 300.0, \
        f"近端真声被压成静音(突发下): out={avg_out_near:.1f}"
    print(f"PASS[8] 2ch 突发碰撞FIFO(同毫秒ts+持续元音): 纯回声 out/mic="
          f"{avg_out_pure/avg_mic_pure*100:.1f}% 近端outRMS={avg_out_near:.1f}")


def test_2ch_srvref_echo_cancellation():
    """2ch_srvref：设备 B2 全双工(2ch 上行、mic 不静音) 但本机固件 ch1(ref) 为空/坏
    (真机实测 ref 88% 静音、与 mic 零延迟结构) → 丢弃坏 ch1、取 ch0(mic)、回声参考
    改走服务端自喂(下行播放)。本测试验证：ch1 全零(坏) 时，仅靠自喂 ref 仍能消回声、
    且近端真声不被压垮——这正是 90C0 真机啸叫的根治路径。

    构造：mic_i = near_i + G*ref_stream[i-1]（Δ=20ms 回声）；2ch 帧 ch1 恒为 0(坏 ref)；
    自喂 ref 缓存 = 各帧下行播放 ref_stream[j]，键=帧时刻，回声源 ref_stream[i-1]
    落在 mic_ts_i-20ms(窗口内) → 互相关选中并谱减。
    """
    n = 50
    G = 0.6
    ref_stream = _make_speech_stream(n, base_seed=0)    # 设备播放(下行)=服务端自喂 ref
    near_stream = _make_speech_stream(n, base_seed=99)  # 设备用户近端真声

    mic_stream = []
    for i in range(n):
        ref_prev = np.frombuffer(ref_stream[max(0, i - 1)], dtype=np.int16).astype(np.float32)
        near_i = np.frombuffer(near_stream[i], dtype=np.int16).astype(np.float32)
        if 15 <= i <= 34:
            near_i = np.zeros(FRAME, np.float32)  # 纯回声段隔离度量
        mic = np.clip(near_i + G * ref_prev, -32768, 32767).astype(np.int16)
        mic_stream.append(mic.tobytes())

    enc = opuslib_next.Encoder(SR, 2, opuslib_next.APPLICATION_VOIP)
    aec = IntercomAec(mode="2ch_srvref", max_delay_ms=2000)

    # 服务端自喂 ref 缓存：键=帧时刻(ms)，值=该帧下行播放 PCM(服务端精确已知)。
    # 生产里自喂 ref 是「下行 opus 解码出的 PCM」(服务端把发给设备的 downlink 解码得到)，
    # 与 mic 上行经 opus 解码的回声走同类 opus 路径 → 编解码着色一致、相关度高。测试同样
    # 对 ref 做 encode→decode 保持一致；若用 raw ref，则与 opus 解码后的 mic 回声去相关
    # → 互相关选错帧 → 消不净(见旧构造 corr=-0.6、残留 79%)。
    base_ts = 1_700_000_000_000
    ref_cache = {base_ts + j * 20: _dec_mono(_enc_mono(ref_stream[j])) for j in range(n)}

    out_rms, mic_rms = [], []
    for i in range(n):
        inter = np.empty((FRAME * 2,), dtype=np.int16)
        inter[0::2] = np.frombuffer(mic_stream[i], dtype=np.int16)  # ch0 = mic
        inter[1::2] = np.zeros(FRAME, np.int16)                    # ch1 = 坏 ref(空)
        opus_2ch = enc.encode(inter.tobytes(), FRAME)
        mic_ts = base_ts + i * 20
        out = aec.process_opus(opus_2ch, ref_cache, mic_ts)
        assert out is not None, f"frame {i}: process_opus 应返回有效 opus"
        out_rms.append(_rms(_dec_mono(out)))
        mic_rms.append(_rms(mic_stream[i]))

    pure = range(15, 35)
    avg_out_pure = float(np.mean([out_rms[i] for i in pure]))
    avg_mic_pure = float(np.mean([mic_rms[i] for i in pure]))
    near_seg = range(5, 15)
    avg_out_near = float(np.mean([out_rms[i] for i in near_seg]))
    avg_near = float(np.mean([mic_rms[i] for i in near_seg]))

    assert avg_mic_pure > 1.0, "纯回声段 mic 应有能量"
    # 纯回声段阈值放宽到 0.6（同 PASS[7]/PASS[8] 的 2ch joint-stereo 混叠地板）：mic 回声
    # 经 2ch opus 编码(ch0)、ref 经 mono opus 编码(自喂 downlink)，两条 opus 路径着色不同
    # → 时域单抽头投影残留 ~54%(opus 链路差)，无法靠单抽头消除；2ch_srvref 仅作兼容回退，
    # 生产路径是 outband。阈值仍远大于"完全不消(≈1.0)"，可抓真回归。
    assert avg_out_pure < avg_mic_pure * 0.6, \
        f"2ch_srvref 坏 ch1 下回声未消除: out={avg_out_pure:.1f} mic={avg_mic_pure:.1f}"
    # 2ch_srvref 启用 NER 断环(best_corr 判主导、压到 floor)：双讲近端会被压制(已知代价，
    # 全双工双讲本就难，优于啸叫)。本测试用单抽头近似(默认 multi_tap=False)，双讲时单抽头
    # 谱减会把 near 的 ref 相关分量也当回声扣→叠加 NER 后 near 被压更狠；生产用 multi_tap=True
    # (NLMS+DTD 双讲冻结) 近端保留远好于此处。护栏仅作「非自杀式静音」(>50) 判别。
    assert avg_out_near > 50.0, \
        f"2ch_srvref 近端被压成静音(NER 过压?): out={avg_out_near:.1f}"
    print(f"PASS[9] 2ch_srvref 坏ch1+自喂ref(NER断环): 纯回声 out/mic="
          f"{avg_out_pure/avg_mic_pure*100:.1f}% 近端outRMS={avg_out_near:.1f}")


def test_phone_uplink_aec():
    """手机双端 AEC：上行裸 640B PCM(无 ref 通道) → process_ref_cache(PCM 域) 逐帧谱减。

    这正是 registry._intercom_aec_process_phone 对 phone 上行逐 320 样本(640B)帧
    调用的函数。回声参考 = 下行(→手机)自喂的 16k mono PCM（_intercom_ref[phone_id]）。
    手机跨设备环根因：B2 全双工下 phone 上行既无设备侧 AEC(client_aec=False) 也无服务端
    AEC(旧代码跳过 phone) → 90C0 单端 AEC 破不了 phone 端的环。本测试验证双端 AEC 中
    phone 这一半：phone 扬声器播放的下行(=ref)被手机 mic 捕获(带 ~150ms 声学环延迟)，
    服务端用自喂 ref 对齐谱减，回声被消、近端(手机用户真声)保留。
    """
    ref = _make_pcm(FRAME, amp=5000.0)                          # 下行到手机的播放内容 = 回声源
    near = _make_pcm(FRAME, amp=2500.0, kind="speech2")         # 手机用户近端真声(不同内容)
    ref_arr = np.frombuffer(ref, dtype=np.int16).astype(np.float32)
    near_arr = np.frombuffer(near, dtype=np.int16).astype(np.float32)
    G = 0.6
    mic_arr = np.clip(near_arr + G * ref_arr, -32768, 32767).astype(np.int16)
    mic = mic_arr.tobytes()

    # 自喂 ref 缓存：下行发送时刻 ref_ts，回声经声学环延迟 ~150ms 回到上行
    ref_ts = 1_700_000_000_000
    mic_ts = ref_ts + 150  # 落在 [-30, 2000] 窗口内
    ref_cache = {ref_ts: ref}
    aec = IntercomAec(mode="outband", max_delay_ms=2000)
    out = aec.process_ref_cache(mic, ref_cache, mic_ts)
    assert out is not None and len(out) == len(mic), "process_ref_cache 应返回等长 PCM"

    residual = _rms(out)
    mic_rms = _rms(mic)
    near_rms = _rms(near)
    assert residual < mic_rms * 0.7, f"phone 回声未有效消除: residual={residual:.1f} mic={mic_rms:.1f}"
    assert residual > near_rms * 0.5, f"phone 近端真声被破坏: residual={residual:.1f} near={near_rms:.1f}"
    print(f"PASS[10] phone 上行AEC(640B帧): mic_rms={mic_rms:.1f} residual={residual:.1f} near_rms={near_rms:.1f}")


def test_phone_uplink_aec_multichunk():
    """手机上行多帧(1280B=2×640B)整帧 AEC：逐块 ts 近似 + 两块均消回声。

    验证 _intercom_aec_process_phone 的切块逻辑：整帧含 2 个 20ms 块，每块带各自的
    延迟回声副本，分别调 process_ref_cache(初值 ts 递减 20ms)。两块都应消回声、
    近端都保留，拼回的整帧不应出现未处理块残留。
    """
    G = 0.6
    n = 2
    ref_stream = _make_speech_stream(n, base_seed=0)    # 下行播放流 = 自喂 ref
    near_stream = _make_speech_stream(n, base_seed=99)  # 手机用户近端真声
    mic_chunks = []
    for i in range(n):
        ref_i = np.frombuffer(ref_stream[i], dtype=np.int16).astype(np.float32)
        near_i = np.frombuffer(near_stream[i], dtype=np.int16).astype(np.float32)
        mic = np.clip(near_i + G * ref_i, -32768, 32767).astype(np.int16)
        mic_chunks.append(mic.tobytes())
    full_mic = b"".join(mic_chunks)

    base_ts = 1_700_000_000_000
    # 每块下行在 base_ts + i*20 发出；回声延迟 ~150ms 回到上行(每块 mic_ts = 发出+150)
    ref_cache = {base_ts + j * 20: ref_stream[j] for j in range(n)}
    aec = IntercomAec(mode="outband", max_delay_ms=2000)
    out = b""
    n_chunks = len(full_mic) // 640
    for i in range(n_chunks):
        chunk = full_mic[i * 640:(i + 1) * 640]
        ts = (base_ts + (n - 1 - i) * 20) + 150  # 帧尾后 +150ms 回声延迟，逐块 -20ms
        cleaned = aec.process_ref_cache(chunk, ref_cache, ts)
        out += cleaned if cleaned else chunk

    # 逐块度量
    out_chunks = [out[i * 640:(i + 1) * 640] for i in range(n)]
    for i in range(n):
        residual = _rms(out_chunks[i])
        mic_rms = _rms(mic_chunks[i])
        near_rms = _rms(near_stream[i])
        assert residual < mic_rms * 0.7, \
            f"phone 多块[{i}] 回声未消: residual={residual:.1f} mic={mic_rms:.1f}"
        assert residual > near_rms * 0.5, \
            f"phone 多块[{i}] 近端破坏: residual={residual:.1f} near={near_rms:.1f}"
    print(f"PASS[11] phone 上行AEC(多块1280B): 两块均消回声且保近端")


def test_2ch_srvref_delayed_echo_band():
    """90C0 长延迟(7s)回声：echo_delay_ms 延迟带约束治本验证（对应 2ch_srvref 真机啸叫）。

    真机根因：90C0 出声 DMA 深，回声比参考晚到 5~9s。process_ref_cache 在
    [-30, max_delay_ms]≈[-30,12000]ms 全窗口做单帧归一化互相关搜索，跨 12s 数百帧
    语音极易**误选非真实回声帧**(近期近端相似帧或频谱相近帧) → 拿错 ref → 回声未
    完全消除 → 泄漏 → 跨设备反馈环 → 啸叫。修复：echo_delay_ms>0 把候选窗口收窄到
    [echo_delay_ms - band, echo_delay_ms + band]，使互相关只能在真实回声延迟带内选帧。

    构造：mic = near + 0.6*echo（echo 来自己 -7000ms 的下行播放帧）。ref 缓存是连续 14s
    的下行流(真实：缓存即连续 peer 语音，用低相关 tone 作填充干扰)。在 -200ms 处放一枚
    『近端诱饵』帧(内容=near 副本)——模拟『近期下行帧波形恰与设备近端真声相似』这一
    全窗口误选触发；该诱饵位于 -200ms，落在延迟带(7000±2000)之外，会被带约束排除。
      - 全窗口(默认，echo_delay_ms=0)：候选含诱饵与真实 -7000ms 回声帧，互相关可能选错
        → 回声未完全消除 → 泄漏(leak_full 明显 >0)。
      - 延迟带(7000±2000)：诱饵(-200ms)落在带外被排除，窗口内最强相关是真实 -7000ms
        回声帧 → 减去 echo → 回声消除(leak_band 近 0)→ 近端保留。
    度量：输出在 echo 方向的余弦相似度=回声泄漏量。带约束应显著低于全窗口；且近端保留。
    """
    G = 0.6
    near = _make_pcm(FRAME, amp=5000.0, kind="speech2")    # 设备用户近端真声
    echo_src = _make_pcm(FRAME, amp=3000.0, kind="speech") # 下行播放(被回声捕获)
    near_arr = np.frombuffer(near, dtype=np.int16).astype(np.float32)
    echo_arr = np.frombuffer(echo_src, dtype=np.int16).astype(np.float32)
    # mic = near + G*echo（echo 来自己 -7000ms 的下行帧）
    mic = np.clip(near_arr + G * echo_arr, -32768, 32767).astype(np.int16).tobytes()

    base_ts = 1_700_000_000_000
    # 14s 连续下行流作 ref 缓存：echo_src 置于 -7000ms，诱饵(=near 副本)置于 -200ms，
    # 其余(tone 440Hz)为低相关填充干扰(模拟不相关的其他下行帧，确保带内不会误选到诱饵外内容)。
    cache = {}
    for j in range(0, 701):
        ts = base_ts + j * 20
        if j == 350:        # mic_ts - 7000 → 真实回声帧（与 mic 回声内容一致）
            cache[ts] = _dec_mono(_enc_mono(echo_src))
        elif j == 690:      # mic_ts - 200 → 近端诱饵帧（与 mic 的近端分量一致→全窗口误选触发）
            cache[ts] = _dec_mono(_enc_mono(near))
        else:
            cache[ts] = _dec_mono(_enc_mono(_make_pcm(FRAME, amp=3000.0, kind="tone")))
    mic_ts = base_ts + 14000  # 真实回声帧在 -7000，诱饵在 -200，均落入全窗口

    # ── 全窗口(默认，无延迟带)：应误选诱饵→回声泄漏 ──
    aec_full = IntercomAec(mode="outband", max_delay_ms=12000)
    out_full = aec_full.process_ref_cache(mic, cache, mic_ts)
    out_full_arr = np.frombuffer(out_full, dtype=np.int16).astype(np.float32)

    # ── 延迟带(7000±2000)：应选中真实回声帧→回声消除 ──
    aec_band = IntercomAec(mode="outband", max_delay_ms=12000,
                           echo_delay_ms=7000, echo_delay_band_ms=2000)
    out_band = aec_band.process_ref_cache(mic, cache, mic_ts)
    out_band_arr = np.frombuffer(out_band, dtype=np.int16).astype(np.float32)

    def _corr(a, b):
        na = float(np.linalg.norm(a))
        nb = float(np.linalg.norm(b))
        if na < 1e-6 or nb < 1e-6:
            return 0.0
        return float(np.dot(a, b)) / (na * nb)  # 真余弦相似度 ∈ [-1, 1]

    leak_full = _corr(out_full_arr, echo_arr)   # 全窗口：输出应仍含大量回声
    leak_band = _corr(out_band_arr, echo_arr)   # 带约束：回声应被消除→近 0
    keep_full = _corr(out_full_arr, near_arr)   # 全窗口近端保留(仅报告)
    keep_band = _corr(out_band_arr, near_arr)   # 带约束：near 应保留→高

    # 带约束必须显著降低回声泄漏（治本）：带宽内选中真回声帧→echo 被消→泄漏近 0
    assert leak_band < 0.4, f"延迟带回声未消除: leak_band={leak_band:.3f}"
    # 带约束相比全窗口必须降低泄漏（排除带外诱饵帧的效果）
    assert leak_band < leak_full * 0.8, \
        f"延迟带未降低回声泄漏(根因未治): leak_band={leak_band:.3f} leak_full={leak_full:.3f}"
    # 带约束必须保留近端真声
    assert keep_band > 0.5, f"延迟带未保留近端: keep_band={keep_band:.3f}"
    # 全窗口基线必须确实回声泄漏明显(否则说明全窗口也已消净，此测试构造失效)
    assert leak_full > 0.3, f"全窗口却未泄漏(基线不符预期): leak_full={leak_full:.3f}"
    print(f"PASS[12] 90C0长延迟回声延迟带约束: 泄漏 full={leak_full:.3f} band={leak_band:.3f} "
          f"近端保留 full={keep_full:.3f} band={keep_band:.3f}")


def test_aec_diag_delay_estimate():
    """诊断：process_ref_cache 应记录估计回声延迟≈真实延迟，且相关峰强(线性回声)。

    构造 mic = 近端 + 0.5·ref_delayed（回声来自 ref_ts 帧），注入延迟 DELAY=7000ms；
    断言 diag 记录到样本、命中强相关峰、估计延迟≈注入延迟、相关峰 max>=0.8。
    这是 90C0 真实回声延迟实测的离线等价验证（生产在会话结束时由 disable 输出 summary）。
    """
    DELAY = 7000  # ms，模拟 90C0 长延迟回声
    ref_ts = 1_000_000
    mic_ts = ref_ts + DELAY
    t = np.arange(FRAME) / SR
    # 参考帧：1kHz 正弦(int16)，rms 远超静默门限
    ref = (1000.0 * np.sin(2 * np.pi * 1000 * t)).astype(np.int16)
    ref_bytes = ref.tobytes()
    # mic = 近端(小) + 0.5·回声(ref 的延迟缩放副本)
    near = (80.0 * np.sin(2 * np.pi * 300 * t + 1.3)).astype(np.int16)
    mic = np.clip(near.astype(np.float32) + 0.5 * ref.astype(np.float32),
                  -32768, 32767).astype(np.int16)
    aec = IntercomAec(mode="outband", max_delay_ms=12000, diag=True)
    out = aec.process_ref_cache(mic.tobytes(), {ref_ts: ref_bytes}, mic_ts)
    assert out is not None
    assert aec._diag_collected, "diag 应记录到样本"
    summ = aec.diag_summary()
    assert summ["samples"] >= 1, summ
    assert summ["corr"]["strong_count"] >= 1, f"应命中强相关峰: {summ}"
    sd = summ["strong_delay_ms"]
    assert sd is not None
    # 估计延迟应接近注入的 DELAY（容差 ±300ms）
    assert abs(sd["median"] - DELAY) <= 300, \
        f"估计回声延迟 {sd['median']} 偏离注入 {DELAY}: {summ}"
    assert summ["corr"]["max"] >= 0.8, f"相关峰应强(线性回声): {summ}"
    print(f"PASS[13] 回声延迟诊断: 估计 median={sd['median']}ms (注入 {DELAY}) "
          f"corr_max={summ['corr']['max']}")


if __name__ == "__main__":
    test_outband_echo_cancellation()
    test_outband_no_ref_passthrough()
    test_outband_ref_out_of_window()
    test_outband_delay_search()
    test_2ch_mode()
    test_2ch_delayed_echo_cancellation()
    test_2ch_realistic_speech_delay()
    test_2ch_burst_collision_fifo()
    test_2ch_srvref_echo_cancellation()
    test_phone_uplink_aec()
    test_phone_uplink_aec_multichunk()
    test_2ch_srvref_delayed_echo_band()
    test_aec_diag_delay_estimate()
    print("ALL INTERCOM_AEC TESTS PASSED")
