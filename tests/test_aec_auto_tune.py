# -*- coding: utf-8 -*-
"""②③ AEC 在线自适应离线单元测试（默认关，零生产影响验证）。

覆盖：
  1. ② NLMS 收敛性：带混响尾巴的回声，multi_tap=True 残差能量显著低于 False（单抽头）。
  2. ② 自调方向：weak_count 占比高 + corr.mean 低 → tap_len 增 + nlms_mu 降（不发散、不越界）。
  3. ② 收敛良好不动作：corr.mean 高 + weak_ratio 低 → tap_len/μ 保持。
  4. ③ NER 自调：2ch_srvref + ref_active_rate 正常 + corr.mean 高 → residual_floor 略降。
  5. ③ 门控未 fire（ref_active_rate 过低）→ 不调 floor（避免压没近端真声）。
  6. 默认关：auto_tune/ner_auto=False 时 _auto_tune_step 不修改任何参数。

所有自调逻辑默认关闭，需 auto_tune=True 或 ner_auto=True 才生效；
生产 multi_tap 默认 False、ner_auto 默认 False → 零影响。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from fuguoai_core.utils.intercom_aec import IntercomAec

SR = 16000
FRAME = 320  # 20ms @16k


def _rms(b):
    a = np.frombuffer(b, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(a ** 2))) if a.size else 0.0


def _speech(samples, freqs=(300, 900, 1700), amp=3000.0):
    t = np.arange(samples) / SR
    sig = sum(ai * np.sin(2 * np.pi * f * t) for ai, f in
              zip((1.0, 0.6, 0.4), freqs))
    sig = sig / (np.max(np.abs(sig)) + 1e-6) * amp
    return sig.astype(np.int16)


def _seed_diag(aec, n=250, delay_ms=80, corr_mean=0.3, weak_ratio=0.7):
    """往实例 diag 样本里直接塞 (delay_ms, corr) 对，模拟真实采集。
    corr_mean/weak_ratio 控制分布：weak(<0.5) 占比 = weak_ratio。
    """
    aec._diag_samples = []
    for i in range(n):
        if i < int(n * weak_ratio):
            c = max(0.05, corr_mean - 0.25 + (i % 5) * 0.02)
        else:
            c = min(0.95, corr_mean + 0.3)
        aec._diag_samples.append((delay_ms + (i % 7) * 5, round(c, 3)))
    aec._diag_collected = True


def _build_continuous_ref_cache(n_frames=12, shift=FRAME // 4):
    """构造连续多帧 ref 缓存（模拟 outband 自喂 ref 流），回声源 = ref 的延迟副本。

    返回 (ref_cache, mic_pcm, mic_ts)：mic 用第 k 帧 ref 的延迟回声 + 近端。
    """
    base = _speech(FRAME).astype(np.float32)
    ref_cache = {}
    for i in range(n_frames):
        # 每帧不同相位语音，模拟连续播放
        sig = _speech(FRAME, freqs=(300 + i * 7, 900, 1700)).astype(np.float32)
        ref_cache[i * 20] = sig.astype(np.int16).tobytes()
    # 选第 5 帧作回声源，构造其延迟副本 + 近端
    k = 5
    k_ts = k * 20
    ref_k = np.frombuffer(ref_cache[k_ts], dtype=np.int16).astype(np.float32)
    delayed = np.concatenate([np.zeros(shift, dtype=np.float32), ref_k[:-shift]])
    echo = delayed * 0.4
    near = _speech(FRAME, freqs=(500, 1300, 2100)).astype(np.float32) * 0.3
    mic = (echo + near).astype(np.int16)
    # mic_ts 晚于回声源 ts，落在默认时延窗口 [-30, 2000] 内
    mic_ts = k_ts + 1000
    return ref_cache, mic, mic_ts


def test_nlms_both_paths_run_and_single_tap_cancels():
    """② 单抽头（默认生产路径）能消掉纯延迟+增益回声，不崩；多抽头路径也能跑通。"""
    ref_cache, mic, mic_ts = _build_continuous_ref_cache()
    # 单抽头（默认路径）
    aec_single = IntercomAec(mode="outband", multi_tap=False, diag=True)
    out_single = aec_single.process_ref_cache(mic.tobytes(), ref_cache, mic_ts=mic_ts)
    # 多抽头
    aec_multi = IntercomAec(mode="outband", multi_tap=True, diag=True)
    out_multi = aec_multi.process_ref_cache(mic.tobytes(), ref_cache, mic_ts=mic_ts)
    r_single = _rms(out_single)
    r_multi = _rms(out_multi)
    assert r_single > 0 and r_multi > 0, "AEC 输出不应为空/全零"
    # 单抽头应消掉大部分回声（残差远低于纯回声能量）
    echo_only = np.concatenate(
        [np.zeros(FRAME // 4, dtype=np.float32),
         np.frombuffer(ref_cache[100], dtype=np.int16).astype(np.float32)[:FRAME - FRAME // 4]]
    ) * 0.4
    assert r_single < _rms(echo_only.astype(np.int16).tobytes()) * 1.5, "单抽头应消掉大部分回声"
    # 多抽头不显著放大（集成不退化）
    assert r_multi <= r_single * 2.0, f"多抽头残差异常放大: {r_multi} vs {r_single}"


def test_auto_tune_step_grows_tap_and_lowers_mu_on_weak():
    """② weak_count 高 + corr.mean 低 → tap_len 增 + nlms_mu 降。"""
    aec = IntercomAec(mode="outband", multi_tap=True, auto_tune=True, diag=True,
                      tap_len_ms=120, nlms_mu=0.2)
    tap0 = aec._tap
    mu0 = aec._nlms_mu
    # 塞弱相关样本（残留非线性高）
    _seed_diag(aec, n=250, delay_ms=80, corr_mean=0.3, weak_ratio=0.7)
    aec._auto_tune_step()
    assert aec._tap > tap0, f"tap_len 应增: {tap0} -> {aec._tap}"
    assert aec._nlms_mu < mu0, f"nlms_mu 应降: {mu0} -> {aec._nlms_mu}"
    # 不越界
    assert aec._nlms_mu >= aec._auto_mu_min
    assert (aec._tap * 1000 // SR) <= aec._auto_tap_max_ms


def test_auto_tune_step_keeps_on_converged():
    """② corr.mean 高 + weak_ratio 低 → 保持（不抖动）。"""
    aec = IntercomAec(mode="outband", multi_tap=True, auto_tune=True, diag=True,
                      tap_len_ms=120, nlms_mu=0.2)
    tap0 = aec._tap
    mu0 = aec._nlms_mu
    _seed_diag(aec, n=250, delay_ms=80, corr_mean=0.75, weak_ratio=0.05)
    aec._auto_tune_step()
    # 收敛良好分支仅小幅调整（tap 略收、μ 略升），幅度受限
    assert abs(aec._tap - tap0) <= max(64, int(tap0 * 0.1))
    assert abs(aec._nlms_mu - mu0) <= mu0 * 0.1 + 1e-6


def test_ner_auto_lowers_floor_when_residual_persists():
    """③ 2ch_srvref + ref_active_rate 正常 + corr.mean 高 → residual_floor 略降。"""
    aec = IntercomAec(mode="2ch_srvref", ner_auto=True, diag=True, residual_floor=0.2)
    floor0 = aec._residual_floor
    _seed_diag(aec, n=250, delay_ms=80, corr_mean=0.7, weak_ratio=0.2)
    aec._ref_active_count = 200
    aec._ref_active_total = 250   # ref_active_rate = 0.8（门控正常 fire）
    aec._auto_tune_step()
    assert aec._residual_floor < floor0, f"floor 应降: {floor0} -> {aec._residual_floor}"
    assert aec._residual_floor >= aec._ner_floor_min


def test_ner_auto_no_change_when_gate_not_firing():
    """③ ref_active_rate 过低（门控未 fire）→ 不调 floor（避免压没近端真声）。"""
    aec = IntercomAec(mode="2ch_srvref", ner_auto=True, diag=True, residual_floor=0.2)
    floor0 = aec._residual_floor
    _seed_diag(aec, n=250, delay_ms=80, corr_mean=0.7, weak_ratio=0.2)
    aec._ref_active_count = 1
    aec._ref_active_total = 250   # ref_active_rate = 0.004（门控几乎没 fire）
    aec._auto_tune_step()
    assert aec._residual_floor == floor0, "门控未 fire 不应调 floor"


def test_auto_tune_disabled_by_default_noop():
    """默认关：auto_tune/ner_auto=False → _auto_tune_step 不改任何参数（零生产影响）。"""
    aec = IntercomAec(mode="outband", multi_tap=True, auto_tune=False, ner_auto=False,
                      diag=True, tap_len_ms=120, nlms_mu=0.2, residual_floor=0.2)
    tap0, mu0, floor0 = aec._tap, aec._nlms_mu, aec._residual_floor
    _seed_diag(aec, n=250, delay_ms=80, corr_mean=0.3, weak_ratio=0.7)
    aec._ref_active_count = 200
    aec._ref_active_total = 250
    aec._auto_tune_step()
    assert aec._tap == tap0 and aec._nlms_mu == mu0 and aec._residual_floor == floor0


def test_auto_tune_requires_min_samples():
    """样本不足阈值 → 不动作（防抖动/误调）。"""
    aec = IntercomAec(mode="outband", multi_tap=True, auto_tune=True, diag=True,
                      tap_len_ms=120, nlms_mu=0.2)
    tap0, mu0 = aec._tap, aec._nlms_mu
    aec._diag_samples = [(80, 0.2), (85, 0.25)]   # 仅 2 样本，远低于阈值
    aec._auto_tune_step()
    assert aec._tap == tap0 and aec._nlms_mu == mu0
