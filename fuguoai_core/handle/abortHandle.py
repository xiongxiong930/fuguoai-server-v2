import json
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler
TAG = __name__

# 打断后 ASR 抑制窗口（秒）：丢弃 TTS 回声尾音 + 网络抖动的残余音频，
# 避免把"打断过渡语/回声尾音"误当成用户新一轮输入。窗口后用户新指令正常进入。
# 0.3s -> 0.8s：实测 AI 回声经空气耦合尾音常 >0.3s，旧值会吃掉用户真实打断语音开头
# -> "打断后识别不准/只识别到尾词"。0.8s 覆盖尾音窗口且不影响新指令进入(realtime 下
# 真人插话 resid 800+ 远超此窗口内的回声残差，新指令照常捕获)。
BARGEIN_ASR_SUPPRESS_SEC = 0.8


async def handleAbortMessage(conn: "ConnectionHandler", from_bargein: bool = False, from_button: bool = False):
    conn.logger.bind(tag=TAG).info("Abort message received")
    # ── realtime 全双工下的两条 abort 来源必须区分 ──
    # ① 设备固件 WS abort：realtime 播报期间每句误发的 legacy 半双工信令（SendAbortSpeaking）
    #    → 若照 stop TTS 会把 AI 每句掐成半句（实测"返回不能说完整话"）。realtime 下忽略。
    # ② 后端 AEC barge-in 守卫检测到的【真人插话】(from_bargein=True)：这是全双工唯一正确的
    #    "用户打断 AI" 路径，必须真正停止 TTS + 清缓冲 + 设抑制窗口（同半双工逻辑）。
    if getattr(conn, "client_listen_mode", "") == "realtime" and not from_bargein and not from_button:
        conn.logger.bind(tag=TAG).info(
            "Abort(realtime): 设备legacy信令已忽略, TTS keeps playing（真人打断由后端barge-in驱动）"
        )
        return
    # 设置成打断状态，会自动打断llm、tts任务
    conn.close_after_chat = False
    conn.client_abort = True
    conn.clear_queues()
    # ② 加固：clear_queues 的 reset() 只清队列、不取消后台发送任务（旧任务会挂起在
    #   queue_has_data_event.wait 不退出），这里显式 stop_sending() 取消任务，确保
    #   barge/abort 后固件不再收到任何残留音频帧——否则残留静音/有效帧会刷新固件
    #   Speaking 看门狗(8s)导致其卡死不转 Listening。下一轮播报会重建任务。
    _rc = getattr(conn, "audio_rate_controller", None)
    if _rc is not None:
        _rc.stop_sending()
    # 打断客户端说话状态
    await conn.safe_send(
        json.dumps({"type": "tts", "state": "stop", "session_id": conn.session_id})
    )
    conn.clearSpeakStatus()
    # ⑤ 加固(abort/barge 通用)：固件 "播报间隙" 子态收到单次 stop 会被忽略（要求
    #   device_state_==Speaking 才转 Listening）。在真实停止信令后再补发一次 stop 兜底
    #   ——固件在 Listening 收到为 no-op（幂等无害），在 Speaking 收到则转 Listening，
    #   覆盖 stop 落在间歇窗口被吞的情况。半双工主动 abort 同样需要，否则固件卡
    #   Speaking 不恢复听（2026-07-22 8818 "说话中卡住"根因）。根治仍依赖固件 v1.10.9 静音看门狗。
    await conn.safe_send(
        json.dumps({"type": "tts", "state": "stop", "session_id": conn.session_id})
    )
    # 重置残留回声守卫计数（避免沿用打断前的回声有效计数）
    conn._aec_eff_streak = 0
    suppress_sec = float(conn.config.get("bargein_asr_suppress_sec", BARGEIN_ASR_SUPPRESS_SEC))
    if from_bargein:
        # 真人插话打断：播放期间用户语音经"真人喂入窗口"(_asr_feed_until)从确认帧起喂入 ASR
        # (receiveAudioHandle realtime 分支，播放中也喂)。此处【保留】该缓冲；抑制窗仅在
        # 真人喂入窗口不活跃(纯回声尾音)时生效——用户打断后继续说话不会被吞。
        # 不清空缓冲 -> 用户打断的完整语句被识别（修"打断期间识别不准/只识别到尾词"）。
        conn._asr_suppress_until = time.time() + suppress_sec
        conn.logger.bind(tag=TAG).info(
            f"Abort(barge-in): 保留用户打断语音, 设 {suppress_sec}s 回声尾音抑制窗口"
        )
    else:
        # 设备 abort / 半双工：清空 ASR 缓冲 + 重置 VAD 态，丢弃打断过渡语/回声尾音
        conn.reset_audio_states()
        conn._asr_suppress_until = time.time() + suppress_sec
        conn.logger.bind(tag=TAG).info(
            f"Abort: 已清空ASR缓冲并设 {suppress_sec}s 抑制窗口（丢弃打断尾音）"
        )
    conn.logger.bind(tag=TAG).info("Abort message received-end")
