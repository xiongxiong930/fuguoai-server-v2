"""上行音频接收与对话编排链（复果原生实现，clean-room 重写版）。

职责：把设备上行 PCM 帧经 VAD / barge-in 裁决后喂给 ASR，并把识别到的文本送入聊天
编排（startToChat）。本协议契约由后端定义，固件侧对齐此实现。

本文件为 fuguoai 自有实现：上游 MIT 仅有 176 行极简骨架（空闲断开 / 唤醒恢复 / 绑定引导 /
超量提示），其余 424 行均为 fuguoai 踩过 90C0 / 8818 真实事故后沉淀的防护演进，包括
「realtime 全双工 barge-in 真人插话判定」「服务端 AEC 残差基线」「回声失控设备
（bargein_disabled_devices）解耦兜底」「播放期 / 尾音窗口守卫」「ASR 连续捕获窗口 +
pre-roll 回补」。以下实现以 fuguoai 行为规格为唯一来源重编，未抄 legacy 源。

其中通用骨架（空闲断开、唤醒恢复、绑定引导、超量提示）以 fuguoai 口径重表达，线协议 /
行为不变；演进防护的判定逻辑与阈值逐项保留，仅做的内部符号重命名与控制流重组（见各助手）。
"""

import time
import json
import asyncio
import collections
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler
from fuguoai_core.utils.util import audio_to_data
from fuguoai_core.handle.abortHandle import handleAbortMessage
from fuguoai_core.handle.intentHandler import handle_user_intent
from fuguoai_core.utils.output_counter import check_device_output_limit
from fuguoai_core.handle.sendAudioHandle import send_stt_message, SentenceType
import os
from fuguoai_config.paths import ASSETS_DIR

TAG = __name__


def _echo_runaway_device(conn: "ConnectionHandler") -> bool:
    """回声失控设备（固件音频出声延迟 runway，AEC 在延迟尖峰期物理失效）标记：仅用于"不静音用户声音"。

    realtime 且设备仍在播时，此类设备 ref 持续到达会令 VAD 静音喂帧把用户真声也静音 → 识别不了，
    故跳过静音喂帧，真实麦直喂 VAD / ASR（下方 _decide_voice_and_early）。
    ⚠️ barge-in 不再在此硬关：改由下方 aec_eff 自适应护栏兜底（AEC 命中参考才允许打断）→
    延迟回落到工作区（当前 ~7s，AEC 正常）用户可正常插话；延迟飙到失效区（~28s）aec_eff 自动
    False 不触发，不回退成碎片。由 config bargein_disabled_devices 按 device_id 配置。
    不影响列表外设备（b4b0 等干净设备 barge-in 全功能保留，且本函数对其返回 False 不触 VAD 改动）。
    """
    _cfg = getattr(conn, "config", None)
    if not _cfg:
        return False
    _list = _cfg.get("bargein_disabled_devices", []) or []
    _dev = getattr(conn, "device_id", None)
    return bool(_dev) and _dev in _list


def _play_grace_seconds(conn: "ConnectionHandler", key: str = "bargein_playing_grace_ms", default: int = 10000) -> float:
    """读取 barge-in 播放判定 grace 窗（秒）。支持 per-device 覆盖：

    config['bargein_playing_grace_ms_per_device'][device_id] 优先于全局
    bargein_playing_grace_ms。用于把 #258 grace 窗（默认 10s，致 90C0 '等十几秒'）
    仅对问题设备（90C0）缩到 ~800ms，不动干净设备（b4b0）的 anti-self-excitation 余量。
    """
    _cfg = getattr(conn, "config", None)
    if not _cfg:
        return default / 1000.0
    _per = _cfg.get("bargein_playing_grace_ms_per_device")
    if isinstance(_per, dict):
        _dev = getattr(conn, "device_id", None)
        if _dev is not None and _dev in _per:
            return float(_per[_dev]) / 1000.0
    return float(_cfg.get(key, default)) / 1000.0


def _bargein_play_min_rms(conn: "ConnectionHandler", base_rms: float) -> float:
    """播放期 barge-in 绝对残差门限（#259 自适应）。

    背景：realtime 播报期设备侧 AFE（VOIP AEC）把上行压到 raw~70-120（用户插话声与
    回声一起被压），固定的 700 绝对地板成为 binding 约束——普通"等一下"残差 << 700
    不 fire、只有吼过 700 才触发（20:16:39 实测 resid=775 刚过线即证）。真正防回声
    自激的是 ratio 门（>2.5x 基线）+ EMA 平滑 + 2 帧防抖：回声残差 ~40-60、ratio ~1x，
    本就过不了 2.5x；绝对地板在加了平滑 / 基线冻结（HUMAN_CAP）后已成冗余护栏。
    adaptive 模式把地板降为 max(mult*残差基线, low_floor)：普通话音量即可过线，
    而回声仍被 ratio 门挡住（不动 ratio / EMA / 防抖，回声防护不变）。

    mode=fixed（默认 = 历史 700，零回归） | adaptive（灰度）。
    配置键（data/.config.yaml 顶层）：
      bargein_play_min_rms_mode                全局 fixed|adaptive（默认 fixed）
      bargein_play_min_rms_mode_per_device     {device_id: fixed|adaptive} 灰度覆盖
      bargein_play_min_rms_adaptive_mult       基线倍率（默认 2.5，宜与 bargein_human_ratio_playing 一致）
      bargein_play_min_rms_adaptive_floor      低绝对下限（默认 150）
      bargein_play_min_rms_adaptive_floor_per_device  {device_id: floor} 按设备覆盖低绝对下限
    """
    _cfg = getattr(conn, "config", None)
    if not _cfg:
        return 700.0
    _dev = getattr(conn, "device_id", None)
    _mode = _cfg.get("bargein_play_min_rms_mode", "fixed")
    _per_mode = _cfg.get("bargein_play_min_rms_mode_per_device")
    if isinstance(_per_mode, dict) and _dev is not None and _dev in _per_mode:
        _mode = _per_mode[_dev]
    if _mode != "adaptive":
        return float(_cfg.get("bargein_human_min_rms_playing", 700))
    _mult = float(_cfg.get("bargein_play_min_rms_adaptive_mult", 2.5))
    _floor = _cfg.get("bargein_play_min_rms_adaptive_floor", 150)
    _per_floor = _cfg.get("bargein_play_min_rms_adaptive_floor_per_device")
    if isinstance(_per_floor, dict) and _dev is not None and _dev in _per_floor:
        _floor = _per_floor[_dev]
    _floor = float(_floor)
    if base_rms and base_rms > 0:
        return max(_mult * float(base_rms), _floor)
    return _floor


def _decide_voice_and_early(conn: "ConnectionHandler", pcm_frame) -> tuple:
    """计算本帧 have_voice（VAD 判定），必要时执行唤醒静默副作用。

    返回 (have_voice, early_return)：early_return=True 表示已走"刚唤醒"短路，调用方应直接 return。
    """
    # 🔴 2026-08-03 FIX(#255，治"我说不了长句")：realtime 全双工下，AI 在播时麦里是
    # 自身回声（90C0 固件延迟深 5~9s、AEC 漏网）。若把含回声的原始帧喂进 VAD，会把 VAD
    # 状态机撑在 speaking 态 → Silero 10s 超时强切把用户真声切碎片。故"设备仍在播"
    # （最近 grace 内收到过 audio_playback_ref）时喂【静音帧】（全零）给 VAD：维持状态机推进
    # 又不被回声驱动；下游 have_voice 判静音，回声不进 ASR。AI 停后自然恢复真帧。
    # 该抑制仅 realtime 生效（半双工 / auto / manual 用原始 mic 判 VAD，不受影响）。
    _grace_v = _play_grace_seconds(conn)
    _ref_playing_v = (time.time() - getattr(conn, "_last_playback_ref_at", 0.0)) < _grace_v
    # 🔴 #256 回声失控设备（在 bargein_disabled_devices 列表）不静音用户声音：其固件出声延迟
    # runaway（实测 28s+），ref 持续到达使 _ref_playing_v 长期为真 → 静音喂帧会把用户真声也
    # 静音 → 识别不了。此类设备直接喂真实麦给 VAD，由 AEC 减回声 + 轮次对话兜底。
    if getattr(conn, "client_listen_mode", "") == "realtime" and _ref_playing_v and not _echo_runaway_device(conn):
        have_voice = conn.vad.is_vad(conn, bytes(len(pcm_frame)))
    else:
        have_voice = conn.vad.is_vad(conn, pcm_frame)
    # 如果设备刚刚被唤醒，短暂忽略 VAD 检测
    if hasattr(conn, "just_woken_up") and conn.just_woken_up:
        have_voice = False
        # 设置一个短暂延迟后恢复 VAD 检测
        _vrt = getattr(conn, "vad_resume_task", None)
        if _vrt is None or _vrt.done():
            conn.vad_resume_task = asyncio.create_task(_resume_vad_after_wake(conn))
        return have_voice, True

    # realtime 全双工：AEC 残差仍含回声底噪（播放时可达 ~170RMS），会让 VAD（silero）
    # 误判"持续语音"（实测 15:34:23 VAD 超时保护 speech_prob_max=0.000 却持续语音 10s）。
    # 残差低于真人阈值（150RMS）时直接判静音，根除回声残差驱动的"自聊 / 自打断"。
    # 仅 realtime 生效，不影响半双工 / auto / manual（VAD 用原始 mic）。
    # 🔴 #256 回声失控设备不静音用户：residual<150 静音抑制会在此类设备上把用户真声（与
    # 回声同被 AEC 压低）一并判静音 → 识别不了。列表内设备跳过此抑制，真实麦直喂 VAD / ASR。
    if getattr(conn, "client_listen_mode", "") == "realtime" and not _echo_runaway_device(conn) and getattr(conn, "_aec_residual_rms", 0.0) < 150:
        have_voice = False
    return have_voice, False


def _gather_bargein_state(conn: "ConnectionHandler", have_voice: bool = False):
    """计算本帧 barge-in 真人插话裁决所需全部状态量，并就地更新连接的 streak / 播放追踪属性。

    返回命名空间，供 _maybe_fire_bargein / _feed_asr_capture_window 读取；不做任何打断或喂帧动作。
    """
    # 服务端 AEC 消回声已在 connection 层完成；barge-in 打断由 client_aec_bargein 控制（阶段2开启）
    # 残留回声守卫：用 AEC 前原始麦能量 vs 自适应回声基线判定真人插话（_human_speech），
    # 不再要求 _aec_eff_streak>=2（该条件与真人插话互斥，会锁死打断）。
    # 纯回声时 raw≈base → human=False 不会自打断；真人声明显超过回声（base*1.4）才打断。
    aec_eff = getattr(conn, "_aec_last_effective", False)
    if aec_eff:
        conn._aec_eff_streak = getattr(conn, "_aec_eff_streak", 0) + 1
    else:
        conn._aec_eff_streak = 0
    # 真人插话判定：改用 AEC 残差能量（而非原始麦能量）vs 自适应残差基线。
    # 原理：AEC 把 AI 回声压平 → 纯回声时残差很低且平稳（无瞬态尖峰）；
    # 真人声（不在参考中）会显著抬高残差。故"残差 vs 残差基线"可干净区分真人插话
    # 与 AI 回声，根除"AI 响亮音节 / 回声尾音被当成真人 → 自打断"的顽疾。
    # （原始麦能量仍保留在 _raw_mic_rms，仅作诊断对比用，不再参与真人判定）
    # 真人判定用【平滑】残差（去音节级抖动，详见 connection._apply_aec 的 EMA）；
    # 缺失时回退瞬时残差，保证向后兼容。改用平滑残差后，真人持续说话时 _human_speech_now
    # 稳定为真 → hstreak 累积触发打断、并打开 ASR 连续捕获窗口（治"打断不了 + 识别不准"）。
    _raw_rms = getattr(conn, "_aec_residual_rms_smooth", getattr(conn, "_aec_residual_rms", 0.0))
    _base = getattr(conn, "_aec_residual_baseline_rms", 0.0)
    _baseline_ready = getattr(conn, "_baseline_ready", False)
    # 真人插话判定阈值收口到 config（默认值与原有硬编码一致，联调可调）：
    # bargein_human_ratio  —— 真人麦能量需超过回声基线的倍数
    # bargein_human_min_rms —— 真人麦能量绝对值下限，滤掉极小底噪误判
    _cfg = getattr(conn, "config", None)
    # 提前计算"AI 音频是否正在下发"（速率控制器队列非空）——真正可靠的"AI 正在播报"信号。
    # 必须早于下方真人插话门限，因为播放中需抬高打断门限（抑制回声残差误触发自激）。
    _rc = getattr(conn, "audio_rate_controller", None)
    # 🔴 2026-08-03 FIX(#254)：后端 TTS 队列空 ≠ 设备已停声。
    # 90C0 等固件音频管线 DMA 缓冲深，实际出声比"写完队列"晚 5~9s；audio_playback_ref
    # 在"写"时即发，故 ref 持续到达 = 设备仍在播。仅看队列会漏掉这 5~9s 错位尾音窗口，
    # 让残留回声以"非播放期低门限"被误判为真人插话（越改越差的根因）。
    # 用最近一次 ref 到达时间延长 playing 判定，grace 默认 10s 覆盖最坏延迟波动。
    _playing_grace = _play_grace_seconds(conn)
    _ref_playing = (time.time() - getattr(conn, "_last_playback_ref_at", 0.0)) < _playing_grace
    _tts_playing = (_rc is not None and len(_rc.queue) > 0) or _ref_playing
    # 真人插话绝对残差门限：配合【平滑残差】（connection._apply_aec EMA），回声瞬态尖峰
    # 被抹平、平滑后仅 ~210RMS；真人持续说话平滑残差稳定 500~700。门限取 400：
    # - 回声平滑后 210 有 1.9x 裕量，绝不误触发自打断（无自激）；
    # - 真人（≥400）稳定过门，正常 / 中等音量即可打断，不再"要吼"；
    # - 软说话（~380）临界，仍由 ratio 门（>基线*1.5≈255）兜底；阈值可在 config 用
    #   bargein_human_min_rms 联调微调（旧值 650 在平滑前对正常语音偏严致"打断不了"）。
    _human_ratio = _cfg.get("bargein_human_ratio", 1.5) if _cfg else 1.5
    _human_min_rms = _cfg.get("bargein_human_min_rms", 400) if _cfg else 400
    # 播放中回声残留（尤其 AEC 偶发失效的响亮音节）会抬高低频残差 → 误判真人 → 自打断。
    # 播放中抬高打断门限（更高 ratio + 更高绝对值），仅清晰真人插话打断；半句回声被挡。
    # 配合 connection 基线爬升至真实回声电平（~130）：播放中回声尖峰（resid~420）经
    # min_rms 700 挡掉、真人（resid 500~700）经 ratio 2.5 放行 → 自激消除且打断不失灵。
    _play_ratio = _cfg.get("bargein_human_ratio_playing", 2.5) if _cfg else 2.5
    # #259：播放期绝对残差门限改自适应（默认 fixed=700 零回归；adaptive 灰度见 _bargein_play_min_rms）。
    _play_min_rms = _bargein_play_min_rms(conn, _base)
    _human_ratio_eff = _play_ratio if _tts_playing else _human_ratio
    _human_min_rms_eff = _play_min_rms if _tts_playing else _human_min_rms
    # 连续帧防抖：AI 自身 TTS 回声残留（AEC 未消净）会出现【单帧能量尖峰】，
    # 瞬时越过 ratio / min_rms 阈值 → 被误判真人 → 自打断把 AI 的话拦腰切断（"说不完整"）。
    # 回声尖峰是瞬时的、真人插话是持续的：要求 _human_speech 连续 ≥N 帧才真触发。
    # ⚠️ N=3 实测过严（真人插话 hstreak 常停在 1~2，因帧间能量起伏不连续）→ 打断失效；
    # 回退 N=2：绝对门限 650 已挡住单帧回声尖峰（≤409），2 帧防抖足够。
    _human_min_frames = _cfg.get("bargein_human_min_frames", 2) if _cfg else 2
    # 真实插话被 ASR 连续捕获窗口时长（秒）：真人声（已验证 streak>=min_frames）出现后，
    # 在该窗口内（且 AI 未播）持续喂 ASR，不按逐帧能量丢弃轻音帧 → 完整句进入识别。
    # b4b0 17:23 实测 要包皮 / 这个听的 / 这个吗 即旧逻辑逐帧 _human_speech_now 门控所致（断裂音频）。
    _asr_feed_hold = _cfg.get("bargein_asr_feed_hold_sec", 1.5) if _cfg else 1.5
    _human_speech_now = (
        _baseline_ready
        and _base > 0
        and _raw_rms > _base * _human_ratio_eff
        and _raw_rms > _human_min_rms_eff
    )
    if _human_speech_now:
        conn._human_speech_streak = getattr(conn, "_human_speech_streak", 0) + 1
    else:
        conn._human_speech_streak = 0
    # 长连播自打断防护（2026-07-23 P0.4）：AI 单轮长故事连播（10-15句 / 10-15s）期间，
    # 高能量音节会偶发回声残差【连续尖峰】越过 barge-in 门限 → 自打断把长故事拦腰切断。
    # 对策：仅当 AI 已【连续播放 >3s】（长播报）时把 barge-in 连续帧要求 2->3；
    # 短对话 / 正常插话仍用 2（不降灵敏度）。真人主动打断长篇通常持续>180ms（3帧）能过，
    # 零星回声尖峰（单 / 双帧）被 3 帧滤掉；配合下方 pre-roll 回补，abort 晚 1 帧也不掉识别头部。
    if _tts_playing:
        if not getattr(conn, "_tts_playing_prev", False):
            conn._tts_play_start = time.time()
    else:
        conn._tts_play_start = 0.0
    conn._tts_playing_prev = _tts_playing
    _long_play = (
        _tts_playing
        and getattr(conn, "_tts_play_start", 0.0) > 0
        and (time.time() - conn._tts_play_start) > 3.0
    )
    # 🔴 #255 长播放打断守卫（仅回声失控设备启用）：config bargein_long_play_guard_ms>0 时，
    # AI 连续播放 >3s 期间禁 barge-in fire（麦里几乎全是自身延迟回声，aec_eff 仍 True 但
    # 漏网回声被当真人反复 fire 把长故事拦腰切断）。干净设备（b4b0）不设此键（默认0）行为不变。
    _long_play_guard = _long_play and (_cfg.get("bargein_long_play_guard_ms", 0) > 0) if _cfg else False
    # 🔴 2026-07-23 17:11 复测回归（P0.4 撤销）：长连播 min_frames 2->3 复刻了 P0.1 的
    # "打断失效"——讲故事场景=长连播=主场景，等于全程 min_frames=3，真人插话 hstreak
    # 因帧间能量起伏常停在 1~2 → 17:09 部署后零 BARGE-FIRE、"完全没打断"。
    # 恒用 min_frames=2：回声单帧尖峰由绝对门限 650 挡（实测尖峰≤409，1.6x 裕量）。
    _eff_min_frames = _human_min_frames
    _human_speech = conn._human_speech_streak >= _eff_min_frames
    _in_tail = getattr(conn, "_asr_suppress_until", 0.0) > time.time()
    # 播报结束后的回声尾音守卫窗口内，禁止 barge-in（避免 AI 自己回声尾音误触发自打断）
    _in_barge_guard = time.time() < getattr(conn, "_barge_guard_until", 0.0)
    # 诊断（aec_diag_enabled 时，仅 speaking 态节流输出，不影响逻辑）
    if getattr(conn, "config", None) and conn.config.get("aec_diag_enabled", False) \
            and getattr(conn, "client_is_speaking", False):
        _now = time.time()
        if _now - getattr(conn, "_barge_diag_last", 0) >= 1.0:
            conn._barge_diag_last = _now
            conn.logger.bind(tag=TAG).info(
                f"[BARGE-DIAG] have_voice={have_voice} aec_eff={aec_eff} "
                f"streak={getattr(conn, '_aec_eff_streak', 0)} "
                f"speaking={getattr(conn, 'client_is_speaking', False)} tts_playing={_tts_playing} "
                f"fire={bool(conn.client_aec and getattr(conn, 'client_aec_bargein', False) and _human_speech and aec_eff)} "
                f"human={_human_speech} hstreak={getattr(conn, '_human_speech_streak', 0)} "
                f"resid={_raw_rms:.0f} rbase={_base:.0f} pmin={_play_min_rms:.0f} ready={_baseline_ready}"
            )
    from types import SimpleNamespace
    return SimpleNamespace(
        aec_eff=aec_eff, _human_speech=_human_speech, _human_speech_now=_human_speech_now,
        _tts_playing=_tts_playing, _long_play=_long_play, _long_play_guard=_long_play_guard,
        _in_barge_guard=_in_barge_guard, _in_tail=_in_tail, _raw_rms=_raw_rms, _base=_base,
        _baseline_ready=_baseline_ready, _play_min_rms=_play_min_rms,
        _human_ratio_eff=_human_ratio_eff, _human_min_rms_eff=_human_min_rms_eff,
        _eff_min_frames=_eff_min_frames, _asr_feed_hold=_asr_feed_hold,
    )


async def _maybe_fire_bargein(conn: "ConnectionHandler", ctx, pcm_frame) -> None:
    """按 barge-in 守卫条件决定是否触发真人插话打断（fire）。

    仅在 client_aec + barge-in 启用 + 真人插话 streak 达标 + AEC 命中参考 + 非回声失控设备
    + 非自打断风暴 + 非尾音守卫 + 非长播守卫 时 fire；声学判定 fire-eligible 后还要过语义门
    （R1：当前恒透传 ACCEPT，零行为变更）。不返回、不改变控制流。
    """
    if (
        conn.client_aec
        and getattr(conn, "client_aec_bargein", False)
        and ctx._human_speech
        # 🔴 #254b 根治 90C0 误打断死循环：barge-in 必须以 AEC 命中参考(aec_eff)为前提。
        # 旧逻辑用 `and _tts_playing`(播放中才打断) —— 但 90C0 固件出声延迟 5~19s 飘忽，
        # 远超 AEC 匹配窗口(aec_max_delay_ms)，回声漏网(aec_eff=False)、残差=原始回声能量
        # (数千)与真人同量级，而 _tts_playing 因 ref 持续到达常驻 True -> 漏网回声在"播放期"
        # 被当成真人反复 BARGE-FIRE -> 反复 Abort -> 对话全碎(越改越差根因)。
        # 改为 `and aec_eff`：仅当 AEC 命中参考(能区分参考外新能量=真人 vs 漏网回声)才允许
        # 打断。漏网回声(aec_eff=False)一律不打断；真人插话帧 AEC 仍命中(aec_eff=True, 残差为
        # 参考外新能量)故正常 fire，不挡真人。无论延迟怎么涨都不再自激 / 误打断。
        # 🔴 #257 回声失控设备(在 bargein_disabled_devices 列表)不再硬关 barge-in，改由上方
        # aec_eff 自适应护栏兜底：延迟回落到工作区(~7s, AEC 正常)用户可正常插话打断；延迟飙到
        # 失效区(~28s) aec_eff 自动 False 不触发，不回退成碎片。VAD 静音喂帧(46/64行)仍由该列表
        # 控制以保用户识别，与 barge-in 解耦。
        and ctx.aec_eff
        and not _echo_runaway_device(conn)  # 🔴 回声失控设备(90C0)硬关 barge-in fire, 转轮次对话。#257 的 aec_eff 护栏在其上仍误 fire(残差漏网 821 当真人)→自激 abort TTS(10:42 日志 BARGE-FIRE resid=821)。保留 #258 播放期喂麦(无空窗)。干净设备不在列表→行为不变。
        and not getattr(conn, "client_abort", False)  # 防同一轮插话反复触发打断风暴
        and not ctx._in_barge_guard  # 播报刚结束的回声尾音窗口内不打断
        # 🔴 #255 长播放守卫：回声失控设备(config bargein_long_play_guard_ms>0)在 AI 连续
        # 播放>3s 期间禁 fire，避免漏网回声(long_play 标 True 的 BARGE-FIRE)把长故事拦腰切断。
        # 干净设备不设此键(默认0)行为不变；用户插话在 AI 停顿(非 long_play)时正常放行。
        and not ctx._long_play_guard
    ):
        if ctx._tts_playing and conn.client_listen_mode != "manual":
            # 触发点无条件打印能量证据，便于真机精调阈值(区分真人插话 vs 回声误触发)
            conn.logger.bind(tag=TAG).info(
                f"[BARGE-FIRE] 真人插话打断触发 resid={ctx._raw_rms:.0f} rbase={ctx._base:.0f} "
                f"ratio={ctx._raw_rms / ctx._base if ctx._base > 0 else 0:.2f} "
                f"hstreak={getattr(conn, '_human_speech_streak', 0)} min_frames={ctx._eff_min_frames} long_play={ctx._long_play}"
            )
            # ── R1 语义门控（Joy-Duplex 五状态思想轻量化，只做 veto 减法）──
            # 声学已判定 fire-eligible，此处做语义层裁决。enabled=false 时语义门
            # 恒返回 ACCEPT = 全透传，零行为变更；R2 / R3 接入声纹 reject、ASR
            # backchannel 后才真正拦截。绝不新增 fire。
            from fuguoai_core.utils.bargein_semantic_gate import (
                semantic_bargein_decide, BargeState,
            )
            _bstate = await semantic_bargein_decide(conn, pcm_frame, ctx._raw_rms, ctx._base)
            if _bstate in (BargeState.REJECT, BargeState.BACKCHANNEL):
                conn.logger.bind(tag=TAG).info(
                    f"[BARGE-GATE] 语义门拦截 fire state={_bstate.name} "
                    f"resid={ctx._raw_rms:.0f} rbase={ctx._base:.0f}"
                )
            else:
                await handleAbortMessage(conn, from_bargein=True)


async def _feed_asr_capture_window(conn: "ConnectionHandler", ctx, pcm_frame, have_voice) -> Optional[bool]:
    """realtime 全双工下维护 ASR 连续捕获窗口（含 pre-roll 回补），返回本帧 _feed_asr。

    返回 None 表示已提前 return（回声尾音窗口 / 播放期纯回声），调用方不应再下发 receive_audio。
    本函数为 async：preroll 回补帧需在序列正确位置 await 下发。
    """
    # pre-roll 环形缓冲(2026-07-23 P0.4)：始终缓存最近 ~6 帧(含残差能量)，
    # 用于喂入窗口开窗时【回补插话头部】——根治"对不上 / 要说多遍"。
    # 根因：开窗需连续 2 帧残差>650 确认，确认帧 + 开口爬升帧(残差 400~650)在播放期被
    # `elif _tts_playing: return` 丢弃 -> ASR 只拿到句子后半 -> 识别成"这个 / 就这 / 是吧"。
    # 开窗上升沿把 preroll 里【有真人成分的爬升帧】(残差>基线*1.2)补喂进 ASR，
    # 跳过纯回声帧(不自激)。maxlen=6(~360ms)刚够覆盖开口到确认的爬升期。
    if not hasattr(conn, "_asr_preroll"):
        conn._asr_preroll = collections.deque(maxlen=6)
    conn._asr_preroll.append((pcm_frame, ctx._raw_rms))
    # 播报刚结束(下降沿)：设 barge-in 守卫，覆盖 air-coupled 回声尾音窗口(~0.6s)。
    if getattr(conn, "_was_tts_playing", False) and not ctx._tts_playing:
        conn._barge_guard_until = time.time() + 1.2
    conn._was_tts_playing = ctx._tts_playing
    # 播报结束回声尾音窗口(air-coupled 回声仍大)：全屏蔽，避免回声进 ASR 自激
    if time.time() < getattr(conn, "_barge_guard_until", 0.0):
        conn._asr_feed_active_prev = time.time() < getattr(conn, "_asr_feed_until", 0.0)
        return None
    # 真人声(已验证) -> 开 / 续期 ASR 连续喂入窗口
    if ctx._human_speech:
        # 开窗上升沿(上一帧窗口未活跃)：回补 preroll 里有真人成分的爬升帧(捕获插话头部)
        if not getattr(conn, "_asr_feed_active_prev", False):
            _base_now = getattr(conn, "_aec_residual_baseline_rms", 0.0)
            for _pf, _prms in list(conn._asr_preroll)[:-1]:
                if _prms > _base_now * 1.2:
                    await conn.asr.receive_audio(conn, _pf, True)
        # 真人声(已验证 streak>=min_frames)一旦出现即开 / 续期一个 ASR 连续喂入窗口，
        # 窗口内(且 AI 未播)持续喂完整音频 -> ASR 拿到整句 -> 识别准确；窗口仅在 AI 未播时生效，
        # 避免把 AI 自身回声喂进去自激。barge 仍由 _human_speech + _tts_playing 触发(见上方)。
        conn._asr_feed_until = time.time() + ctx._asr_feed_hold
    conn._asr_feed_active_prev = time.time() < getattr(conn, "_asr_feed_until", 0.0)
    # 喂入窗口内持续喂 ASR —— 包括 AI 播放中！
    # 2026-07-23 16:30 复测血泪：旧逻辑 `and not _tts_playing` + `elif _tts_playing: return`
    # 把"插话语音的头部"(从开口到 barge-in 生效、下发队列清空为止)整段丢弃，
    # 叠加 abort 后 0.8s 抑制窗，一句话只剩尾巴 -> 识别成"这样 / 这个 / 就这"(掐头碎片)。
    # 窗口只由【已验证真人声】(streak>=min_frames, resid>650 且 >基线*1.5)开启，
    # 回声残差(尖峰≤409)开不了窗，播放中喂的是 AEC 残差(真人声占主导)，无自激风险。
    if time.time() < getattr(conn, "_asr_feed_until", 0.0):
        _feed_asr = True
    elif ctx._tts_playing:
        if _echo_runaway_device(conn):
            # 🔴 #258(2026-08-05): 回声失控设备(在 bargein_disabled_devices)当前 Δt~10s
            # 内 AEC 有效(residual 低), 播放期也喂真麦 -> 消除"TTS 播完~grace 窗"内用户声被
            # 静默丢弃造成的十几秒空窗。此处喂入的是 AEC 残差 PCM(回声已被压低), 自激风险可忽略。
            _feed_asr = have_voice
        else:
            return None  # 播放中且无真人喂入窗口：纯回声不喂
    else:
        _feed_asr = have_voice
    return _feed_asr


async def handleAudioMessage(conn: "ConnectionHandler", pcm_frame):
    # 当前片段是否有人说话
    have_voice, _early = _decide_voice_and_early(conn, pcm_frame)
    if _early:
        return

    ctx = _gather_bargein_state(conn, have_voice)
    await _maybe_fire_bargein(conn, ctx, pcm_frame)

    # 设备长时间空闲检测，用于 say goodbye
    await _idle_close_or_farewell(conn, have_voice)

    # ── realtime 全双工：用"连续捕获窗口"喂 ASR，根治"识别错误 / 只识别到尾词" ──
    # 仅对 realtime 模式生效，半双工 / auto / manual 行为不变。
    if getattr(conn, "client_listen_mode", "") == "realtime":
        _feed_asr = await _feed_asr_capture_window(conn, ctx, pcm_frame, have_voice)
        if _feed_asr is None:
            return
    else:
        _feed_asr = have_voice

    # 打断(abort)后回声尾音窗口：屏蔽回声尾音 / 网络抖动残余进 ASR。
    # ⚠️ 例外(2026-07-23 16:30 复测血泪)：barge-in 后用户往往【还在继续说话】，
    # 0.8s 全屏蔽会把这段话也吞掉(与播放中丢头部叠加 -> 整句只剩尾巴)。
    # 故 realtime 下若真人喂入窗口活跃(窗口只由已验证真人声开启，回声开不了窗)，
    # 抑制窗让位于真人语音——继续喂 ASR；窗口不活跃(纯尾音)才屏蔽。
    if getattr(conn, "_asr_suppress_until", 0) > time.time():
        if not (
            getattr(conn, "client_listen_mode", "") == "realtime"
            and time.time() < getattr(conn, "_asr_feed_until", 0.0)
        ):
            return

    # 接收音频
    await conn.asr.receive_audio(conn, pcm_frame, _feed_asr)


async def _resume_vad_after_wake(conn: "ConnectionHandler"):
    """唤醒后短暂静默窗口：等 2s 再恢复 VAD 检测，避免唤醒词余韵被当成用户起话。"""
    await asyncio.sleep(2)
    conn.just_woken_up = False


async def startToChat(conn: "ConnectionHandler", text):
    """一轮对话入口：解析多说话人信封、做绑定 / 超量前置校验，再进入意图与聊天编排。

    契约（与固件 / 小程序对齐，改动需同步）：
    - 输入可为多说话人 JSON 信封 {"speaker":..,"content":..}，解析失败回退原文。
    - need_bind 时只做绑定引导；超量时只播超量提示；两者均短路返回。
    - realtime 全双工与 manual 模式下不在此处 abort 在播内容（打断交由 barge-in 守卫）。
    """
    speaker_name = None
    actual_text = text

    try:
        # 尝试解析JSON格式的输入（多说话人信封：{"speaker":..,"content":..}）
        if text.strip().startswith("{") and text.strip().endswith("}"):
            data = json.loads(text)
            if "speaker" in data and "content" in data:
                speaker_name = data["speaker"]
                actual_text = data["content"]
                conn.logger.bind(tag=TAG).info(f"解析到说话人信息: {speaker_name}")
    except (json.JSONDecodeError, KeyError):
        # 如果解析失败，继续使用原始文本
        pass

    # 保存说话人信息到连接对象
    if speaker_name:
        conn.current_speaker = speaker_name
    else:
        conn.current_speaker = None

    if conn.need_bind:
        await check_bind_device(conn)
        return

    # 如果当日的输出字数大于限定的字数
    if conn.max_output_size > 0:
        if check_device_output_limit(
            conn.headers.get("device-id"), conn.max_output_size
        ):
            await _announce_quota_exhausted(conn)
            return

    # manual 模式下不打断正在播放的内容
    # realtime 全双工下也不在此处打断：realtime 麦持续上行，ASR 每识别出一句就进入 startToChat，
    # 若按 client_is_speaking 无条件 abort，会把 AI 每句刚开口就掐断（后端自触发 abort 风暴，非固件）。
    # realtime 的打断交由上游 aec barge-in 守卫处理（用 _human_speech 区分真人声 / 回声，见本文件 barge-in 段）。
    if (
        conn.client_is_speaking
        and conn.client_listen_mode != "manual"
        and conn.client_listen_mode != "realtime"
    ):
        await handleAbortMessage(conn)

    # 首先进行意图分析，使用实际文本内容
    intent_handled = await handle_user_intent(conn, actual_text)

    if intent_handled:
        # 如果意图已被处理，不再进行聊天
        return

    # 准备开始新会话：必须在 send_stt_message 之前复位 client_abort！
    # 血泪根因(2026-07-23 16:16 复测)：barge-in abort 后 client_abort=True，
    # 下一轮 startToChat 先调 send_stt_message —— 它见 client_abort=True 早退，
    # "tts start" 永不下发、client_is_speaking 保持 False -> 固件停在"聆听中"，
    # 服务端照常合成下发 8 句音频+字幕 -> 设备只刷文本不出声(Listening 态丢弃音频)，
    # 且 barge-in 守卫因 speaking=False 全程死掉 -> 语音打断彻底失效(只能按键)。
    conn.client_abort = False

    # 意图未被处理，继续常规聊天流程，使用实际文本内容
    await send_stt_message(conn, actual_text)

    # M2 加固：同一连接严格串行对话，提交新轮前先等待上一轮结束，杜绝快速打断时两段文本叠灌 TTS 串话
    prev_future = getattr(conn, "_chat_future", None)
    if prev_future is not None and not prev_future.done():
        try:
            await asyncio.wait_for(asyncio.wrap_future(prev_future), timeout=30)
        except Exception as e:
            conn.logger.bind(tag=TAG).warning(f"等待上一轮对话结束异常（已继续）: {e}")
    conn._chat_future = conn.executor.submit(conn.chat, actual_text)


async def _idle_close_or_farewell(conn: "ConnectionHandler", have_voice):
    """空闲超时收线：设备持续无语音超过阈值后，自动进入结束语（或静默关连接）。

    契约：阈值读 config.close_connection_no_voice_time（秒，默认 120）；
    close_after_chat 已置位则说明本伦已被其他路径接管，不再重复触发。
    结束语文案优先取 config.end_prompt.prompt，缺省回退为内置告别语。
    """
    if have_voice:
        conn.last_activity_time = time.time() * 1000
        return
    # 仅在已初始化活动时间戳后才做超时判定（避免冷连接误杀）
    if conn.last_activity_time > 0.0:
        no_voice_time = time.time() * 1000 - conn.last_activity_time
        close_connection_no_voice_time = int(
            conn.config.get("close_connection_no_voice_time", 120)
        )
        if (
            not conn.close_after_chat
            and no_voice_time > 1000 * close_connection_no_voice_time
        ):
            conn.close_after_chat = True
            conn.client_abort = False
            end_prompt = conn.config.get("end_prompt", {})
            if end_prompt and end_prompt.get("enable", True) is False:
                conn.logger.bind(tag=TAG).info("结束对话，无需发送结束提示语")
                await conn.close()
                return
            prompt = end_prompt.get("prompt")
            if not prompt:
                prompt = "请你以```时间过得真快```未来头，用富有感情、依依不舍的话来结束这场对话吧。！"
            await startToChat(conn, prompt)


async def _announce_quota_exhausted(conn: "ConnectionHandler"):
    """超量保护：当日输出字数触顶时，播报内置提示并结束本轮对话。

    契约：文案为内置固定语；音频取 ASSETS_DIR/max_output_size.wav；
    以 SentenceType.LAST 入队确保播完即收线（close_after_chat=True）。
    """
    conn.client_abort = False
    text = "不好意思，我现在有点事情要忙，明天这个时候我们再聊，约好了哦！明天不见不散，拜拜！"
    await send_stt_message(conn, text)
    file_path = os.path.join(ASSETS_DIR, "max_output_size.wav")
    opus_packets = await audio_to_data(file_path)
    conn.tts.tts_audio_queue.put((SentenceType.LAST, opus_packets, text))
    conn.close_after_chat = True


async def check_bind_device(conn: "ConnectionHandler"):
    """未绑定设备引导：带 bind_code 则逐位播报绑定码，否则提示 OTA 配置缺失。

    契约：bind_code 须为 6 位；逐位数字音频取自 ASSETS_DIR/bind_code/<d>.wav；
    首段 FIRST、各位 MIDDLE、收尾 LAST([]) 三段式入队，与 TTS 状态机对齐。
    """
    if conn.bind_code:
        # 绑定码格式校验：必须为 6 位
        if len(conn.bind_code) != 6:
            conn.logger.bind(tag=TAG).error(f"无效的绑定码格式: {conn.bind_code}")
            text = "绑定码格式错误，请检查配置。"
            await send_stt_message(conn, text)
            return

        text = f"请登录控制面板，输入{conn.bind_code}，绑定设备。"
        await send_stt_message(conn, text)

        # 播报引导提示音
        music_path = os.path.join(ASSETS_DIR, "bind_code.wav")
        opus_packets = await audio_to_data(music_path)
        conn.tts.tts_audio_queue.put((SentenceType.FIRST, opus_packets, text))

        # 逐位播报绑定码数字（严格 6 位，越界由 try 兜底）
        for i in range(6):
            try:
                digit = conn.bind_code[i]
                num_path = os.path.join(ASSETS_DIR, "bind_code", f"{digit}.wav")
                num_packets = await audio_to_data(num_path)
                conn.tts.tts_audio_queue.put((SentenceType.MIDDLE, num_packets, None))
            except Exception as e:
                conn.logger.bind(tag=TAG).error(f"播放数字音频失败: {e}")
                continue
        conn.tts.tts_audio_queue.put((SentenceType.LAST, [], None))
    else:
        # 未配置 OTA：提示固件需重新编译
        conn.client_abort = False
        text = f"没有找到该设备的版本信息，请正确配置 OTA地址，然后重新编译固件。"
        await send_stt_message(conn, text)
        music_path = os.path.join(ASSETS_DIR, "bind_not_found.wav")
        opus_packets = await audio_to_data(music_path)
        conn.tts.tts_audio_queue.put((SentenceType.LAST, opus_packets, text))
