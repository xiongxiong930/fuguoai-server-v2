"""联网提醒调度器：扫描到点提醒 → TTS 生成 → 经 vm-server 虚拟对端推流到设备。

复用留言播放的同一条「服务端虚拟对端」通道（voicemail_player 同款）：
SetIntercom start(peer=vm-server) → 20ms 实钟节奏逐帧 [4B seq][opus] → stop。
固件零改动；播放期间设备 mic 上行由 vm_sink 静默丢弃。

节奏铁律（60ms 教训）：loop.time() 绝对时刻表，绝不 sleep 累积、绝不突发灌帧。
"""

import asyncio
import logging
import struct
import threading
import time
from collections import deque

from fuguoai_core.utils.voicemail_store import decode_to_pcm, pcm_to_frames
from fuguoai_core.utils.connection_registry import get_connection_registry
from fuguoai_core.utils.intercom_registry import get_intercom_registry, _get_conn
from fuguoai_core.utils.networked_reminder_store import get_networked_reminder_store
from fuguoai_core.utils.voicemail_player import _device_supports_playmessage

TAG = __name__
_logger = logging.getLogger(TAG)

VM_PEER = "vm-server"           # 与留言播放同一虚拟对端（固件据此进对讲态播放）
FRAME_SEC = 0.02                # 20ms/帧
PRE_ROLL_SEC = 0.3             # start 后等设备进对讲态
DRAIN_SEC = 0.5                # 尾帧后排空再 stop
ABORT_CHECK_EVERY = 25         # 每 25 帧（0.5s）查一次抢占
WORKER_INTERVAL = 15.0         # 扫描轮询间隔

# 单 rid 自动触发熔断（2026-10-17 复盘 8818 nr538620498bb0：interval 180s
# 无人看管连响 11.8 天 / 1388 次）。仅计 worker 自动触发，手动「立即播报」
# 不计数（用户主动操作不应被熔断误伤）。窗口内超限 → 自动 disabled + ERROR。
BURST_WINDOW_SEC = 3600
BURST_LIMIT = 6


class BurstGuard:
    """单 rid 在滑动窗口内的自动触发计数与熔断判定（进程内存态，重启清零）。

    interval 900s 的正常节奏 = 每小时 4 次，留 50% 余量定 6 次；
    daily/weekly 每小时最多 1 次，不可能误伤。
    """

    def __init__(self, window=BURST_WINDOW_SEC, limit=BURST_LIMIT):
        self._window = window
        self._limit = limit
        self._hits = {}          # rid -> deque[unix秒]
        self._lock = threading.Lock()

    def record(self, rid, now):
        """记一次自动触发；返回 True=已超限，调用方应熔断。"""
        with self._lock:
            dq = self._hits.setdefault(rid, deque())
            dq.append(now)
            while dq and now - dq[0] > self._window:
                dq.popleft()
            return len(dq) > self._limit

    def reset(self, rid):
        with self._lock:
            self._hits.pop(rid, None)


def _set_intercom_cmd(peer, action):
    return {
        "type": "iot",
        "commands": [{
            "name": "DeviceControl",
            "method": "SetIntercom",
            "parameters": {"peer": peer, "action": action},
        }],
    }


def _device_playable(device_id):
    """在线 + 不在对讲态 + 不在活跃呼叫单。"""
    try:
        from fuguoai_core.utils.intercom_call_store import get_call_orchestrator
        if not get_connection_registry().is_online(device_id):
            return False
        if get_intercom_registry().peer_of(device_id):
            return False
        orch = get_call_orchestrator()
        with orch._lock:
            if device_id in orch._by_device:
                return False
        return True
    except Exception:
        return False


def _still_ok(device_id):
    return _device_playable(device_id)


_TTS = None
# per-voice 腾讯实例缓存（角色出厂/用户调校音色，2026-10-16 设备面音色闭环）。
# 范式同 tts_routes._TENCENT_CACHE：请求声线透传 private_voice，数字串直取
# VoiceType（/tts?voice= 已在 159 生产验证）。voice=None 走上面的系统单例
# （提醒/播报默认声线），历史行为零变化。
_TTS_BY_VOICE = {}
_TTS_VOICE_LOCK = threading.Lock()


def _get_tts(voice=None):
    """懒加载 TTS 实例。

    voice=None → 系统同配置单例（提醒/播报默认声线，复用运行时 config +
    Tencent 兜底）；voice=腾讯 VoiceType 数字串（角色音色）→ per-voice
    腾讯实例（角色出厂/用户调校）。per-voice 构建失败返回 None，由调用方
    回落默认单例（不静音，与 _synth_with_fallback 的兜底哲学一致）。
    """
    global _TTS
    if not voice:
        if _TTS is not None:
            return _TTS
        try:
            from fuguoai_core.utils.cache.manager import cache_manager, CacheType
            from fuguoai_core.utils.modules_initialize import initialize_tts
            cfg = cache_manager.get(CacheType.CONFIG, "main_config")
            if cfg:
                _TTS = initialize_tts(cfg)
                _logger.info("[reminder] TTS 实例就绪")
        except Exception as e:
            _logger.warning(f"[reminder] TTS 懒加载失败(后续重试): {e}")
        return _TTS
    voice = str(voice).strip()
    if not voice:
        return _get_tts()
    with _TTS_VOICE_LOCK:
        p = _TTS_BY_VOICE.get(voice)
    if p is not None:
        return p
    try:
        from fuguoai_core.utils.cache.manager import cache_manager, CacheType
        from fuguoai_core.utils.tts import create_instance
        cfg = cache_manager.get(CacheType.CONFIG, "main_config") or {}
        block = dict((cfg.get("TTS") or {}).get("TencentTTS") or {})
        if not block:
            _logger.warning(f"[reminder] config 无 TencentTTS 段，per-voice 不可用 voice={voice}")
            return None
        block["format"] = "mp3"           # 与 tts_routes._get_tencent 一致：强制 mp3
        block["private_voice"] = voice    # 关键：透传角色声线
        p = create_instance("tencent", block, None)
        with _TTS_VOICE_LOCK:
            _TTS_BY_VOICE[voice] = p
        _logger.info(f"[reminder] per-voice TTS 实例就绪 voice={voice}")
        return p
    except Exception as e:
        _logger.warning(f"[reminder] per-voice TTS 构建失败 voice={voice}: {e}")
        return None


async def _synth_with_fallback(provider, text):
    """主 TTS 合成；失败/空结果时兜底到备用后端(腾讯)，返回 (audio_bytes, file_type)。

    与 base.py 的 to_tts_stream/to_tts 同源：主后端失败即走 _fallback_synth_bytes，
    使设备推送(提醒/在线播报/角色播报)和交互式聊天一样有备份、不再静音。
    主后端(Edge)当前被微软 403 时，自动切腾讯 101002，定时器不再静音。
    """
    try:
        audio = await provider.text_to_speak(text, None)
    except Exception as e:
        _logger.warning(f"[speak] 主 TTS 合成失败, 转兜底: {e}")
        audio = None
    if audio:
        return audio, getattr(provider, "audio_file_type", None) or "mp3"
    fb = getattr(provider, "_fallback_tts", None)
    if fb is None:
        return None, "mp3"
    # 同步调用(内部 _run_async + 熔断)，与 base.py to_tts_stream 完全一致
    fb_audio, fb_ft = provider._fallback_synth_bytes(text)
    if fb_audio:
        _logger.warning(f"[speak] 兜底 TTS 已接管: {text[:48]}")
        return fb_audio, fb_ft or "mp3"
    return None, "mp3"


async def _tts_to_opus(text, voice=None):
    """TTS 文本 → 16k/mono opus 帧列表；失败返回 None。

    voice=角色/调校音色（腾讯 VoiceType）：per-voice 腾讯实例合成；构建或
    合成失败回落系统默认单例（不静音）。voice=None → 原系统单例路径
    （提醒/播报），行为零变化。
    """
    provider = _get_tts(voice)
    if provider is None:
        provider = _get_tts()
        if provider is None:
            return None
    audio, fmt = await _synth_with_fallback(provider, text)
    if not audio and voice:
        # per-voice 合成失败 → 回落默认声线再试一次（不静音）
        provider = _get_tts()
        if provider is not None:
            audio, fmt = await _synth_with_fallback(provider, text)
    if not audio:
        return None
    try:
        pcm = decode_to_pcm(audio, fmt)
    except Exception as e:
        _logger.warning(f"[reminder] TTS 音频解码失败: {e}")
        return None
    if not pcm:
        return None
    try:
        return pcm_to_frames(pcm)
    except Exception as e:
        _logger.warning(f"[reminder] PCM→opus 失败: {e}")
        return None


async def _tts_to_opus_chat(conn, text, voice=None):
    """对话音频通道专用：TTS PCM → 与正常聊天同源的 opus（conn.sample_rate / 60ms）。

    不能用 _tts_to_opus（那产出 16k/20ms，仅 vm-server/对讲通道正确）；对话音频通道
    SetOnServerAudio 与聊天回复同源，设备按 conn.sample_rate(通常 24k)/60ms 解码，
    错用 16k/20ms 会被以 ~1.5x 变速+升调解码（即"推过去的码率不对"反馈）。

    编码用独立的 OpusEncoderUtils（不碰 conn.opus_encoder 共享状态，避免与进行中的聊天
    TTS 串帧），采样率/帧长严格对齐设备，与 sendAudioMessage 的 60ms rate controller 一致。

    率对齐铁律：TTS provider 返回的 PCM 是 decode_to_pcm 解码出的 16k/mono/s16le
    （voicemail_store.SAMPLE_RATE=16000，与 conn.sample_rate 无关），必须显式重采样到
    conn.sample_rate(通常 24k) 后再按该率编码；否则 16k 采样点被当 24k 帧长编码 →
    设备按 24k 解码 → ~1.5x 变速+升调（"推过去的码率不对"反馈）。复用 voicemail_player
    已验证的 _resample_pcm（numpy 线性插值，容器内可用）。

    voice=角色/调校音色（腾讯 VoiceType）：per-voice 腾讯实例；构建或合成失败
    回落系统默认单例（不静音）。voice=None → 原系统单例，行为零变化。
    """
    provider = _get_tts(voice)
    if provider is None:
        provider = _get_tts()
        if provider is None:
            return None
    audio, fmt = await _synth_with_fallback(provider, text)
    if not audio and voice:
        # per-voice 合成失败 → 回落默认声线再试一次（不静音）
        provider = _get_tts()
        if provider is not None:
            audio, fmt = await _synth_with_fallback(provider, text)
    if not audio:
        return None
    try:
        from fuguoai_core.utils.voicemail_store import decode_to_pcm
        # 🔴 铁律：provider 返回的是压缩音频二进制（Edge=mp3 / 兜底腾讯=wav），必须经
        # decode_to_pcm 解码成 16k/mono/s16le PCM，再重采样+编码。漏解码会把压缩数据当
        # PCM 直接编码 → 设备收到乱码且时长按错误字节数算（19296B mp3 被当 0.6s PCM →
        # 仅 11 帧乱码，播完翻 listening 采到用户后半句 → "没听清"死循环）。
        pcm = decode_to_pcm(audio, fmt)  # 16k/mono/s16le PCM（fmt 随主/兜底后端变化）
        if not pcm:
            return None
        from fuguoai_core.utils.voicemail_player import _resample_pcm
        # 16k → conn.sample_rate 显式重采样（关键：否则 16k 数据被当 24k 编码变调）
        pcm_in = pcm
        if conn.sample_rate and conn.sample_rate != 16000:
            pcm = _resample_pcm(pcm, 16000, conn.sample_rate)
        # 诊断：确认 TTS 解码后真实时长 + 重采样前后
        conn.logger.bind(tag="speak").info(
            f"[speak-diag] provider={type(provider).__name__} fmt={fmt} "
            f"audio={len(audio)} pcm16={len(pcm_in)}(~{len(pcm_in)/16000/2:.2f}s) "
            f"pcm_out={len(pcm)}(sr={conn.sample_rate})"
        )
        from fuguoai_core.utils.opus_encoder_utils import OpusEncoderUtils
        enc = OpusEncoderUtils(sample_rate=conn.sample_rate, channels=1, frame_size_ms=60)
        frames = []
        enc.encode_pcm_to_opus_stream(pcm, True, lambda d: frames.append(d))
        if not frames:
            return None
        return frames
    except Exception as e:
        _logger.warning(f"[speak] 对话通道 opus 编码失败(sr={conn.sample_rate}): {e}")
        return None


async def _stream_opus_to_device(device_id, opus_frames, label=""):
    """vm-server 虚拟对端逐帧推流播放；中途设备离线/被抢占返回 False。"""
    reg = get_connection_registry()
    intercom = get_intercom_registry()
    intercom.vm_sink_add(device_id)
    started = False
    try:
        if not await reg.send_to_device(device_id, _set_intercom_cmd(VM_PEER, "start")):
            _logger.info(f"[reminder] 设备不可达,跳过 {label}")
            return False
        started = True
        await asyncio.sleep(PRE_ROLL_SEC)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        seq = 0
        for i, opus in enumerate(opus_frames):
            target = t0 + i * FRAME_SEC
            delay = target - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            if i % ABORT_CHECK_EVERY == 0 and not _still_ok(device_id):
                _logger.info(f"[reminder] 播放被抢占/设备离线,中止 {label} @frame{i}")
                return False
            conn = _get_conn(device_id)
            if conn is None:
                return False
            try:
                await conn.safe_send_bytes(struct.pack(">I", seq) + opus)
            except Exception as e:
                _logger.info(f"[reminder] 帧发送失败 {label} @frame{i}: {e}")
                return False
            seq = (seq + 1) & 0xFFFFFFFF
        await asyncio.sleep(DRAIN_SEC)
        return True
    finally:
        if started:
            try:
                await reg.send_to_device(device_id, _set_intercom_cmd(VM_PEER, "stop"))
            except Exception:
                pass
        intercom.vm_sink_remove(device_id)


_speaking = set()  # 防同设备并发推流(vm-server 单会话)


async def speak_text_to_device(device_id, text, label="speak_request", voice=None):
    """通用：把任意文本合成 TTS 并推流到设备。

    能力探测双路（2026-08-13）：
    - 设备已上报 DeviceControl.PlayMessage → 走对话音频通道（send_tts_message +
      sendAudioMessage，与聊天回复同源；固件 SetOnServerAudio 收帧即播/取消超时回退）。
      用于 v2.0.0-line 等新固件（无 SetIntercom/vm-server）。
    - 否则回退 vm-server / SetIntercom 虚拟对端通道（v1.6.0 等老固件）。
    供三类调用：
      - 联网提醒 fire_one（服务端定时器到点播报，已有）
      - 设备在线时主动请求播报（speak_request：来自固件 PlayMessage / 在线提醒）
      - 角色设备面回复（character_routes：传角色/调校音色 voice）
    voice=腾讯 VoiceType 数字串时按角色音色合成；None=系统默认声线。
    返回 True=已成功推流；False=失败(文本空/设备不可达/TTS合成失败)。
    """
    text = (text or "").strip()
    if not text:
        return False
    if device_id in _speaking:
        _logger.info(f"[speak] 设备正在推流,跳过 {label} did={device_id}")
        return False
    _speaking.add(device_id)
    try:
        conn = _get_conn(device_id)
        if conn is not None and _device_supports_playmessage(conn):
            return await _speak_via_chat(conn, device_id, text, label, voice=voice)
        # 回退 vm-server / SetIntercom
        opus = await _tts_to_opus(text, voice=voice)
        if not opus:
            return False
        return await _stream_opus_to_device(device_id, opus, label=label)
    finally:
        _speaking.discard(device_id)


async def _speak_via_chat(conn, device_id, text, label, voice=None):
    """对话音频通道 TTS（v2.0.0-line 等新固件，无 SetIntercom）。

    与聊天回复同源：start → FIRST(opus+文本) → LAST → stop。
    固件 SetOnServerAudio 回调收帧即播并取消 speak_request 超时本地回退。
    voice=角色/调校音色（腾讯 VoiceType）时按角色音色合成。
    """
    from fuguoai_core.handle.sendAudioHandle import sendAudioMessage, send_tts_message
    from fuguoai_core.providers.tts.dto.dto import SentenceType

    opus = await _tts_to_opus_chat(conn, text, voice=voice)
    if not opus:
        return False
    try:
        await send_tts_message(conn, "start")
        await sendAudioMessage(conn, SentenceType.FIRST, opus, text)
        await sendAudioMessage(conn, SentenceType.LAST, [], None)
        await send_tts_message(conn, "stop", None)
        # 用 conn.logger（INFO 可见），模块级 _logger 被根 WARNING 压制看不到
        conn.logger.bind(tag="speak").info(
            f"[speak] chat 通道 TTS 已下发 {label} did={device_id} "
            f"sr={conn.sample_rate} frames={len(opus)} (~{len(opus)*0.06:.2f}s)"
        )
        return True
    except Exception as e:
        _logger.warning(f"[speak] chat 通道 TTS 失败 {label} did={device_id}: {e}")
        return False


class NetworkedReminderScheduler:
    def __init__(self):
        self._task = None
        self._playing = set()
        self._burst = BurstGuard()

    async def fire_one(self, device_id, rid, text, auto=False):
        """触发单条提醒播放。返回 True=已播报；失败保持 pending 待重试。

        经 speak_text_to_device 双路探测：新固件(v2.0.0-line, 无 SetIntercom)走
        chat/PlayMessage 通道，老固件回退 vm-server/SetIntercom 虚拟对端。固件零改动。
        （此前硬编码 _stream_opus_to_device → 仅老固件出声，新固件到点静默。）

        auto=True（仅 worker 调用）：计入熔断窗口，超限自动禁用该 rid，
        防无人看管的循环提醒连响（2026-10-17 8818 复盘）；手动立即播报不计数。
        """
        now = int(time.time())
        if not _device_playable(device_id):
            get_networked_reminder_store().set_attempt(device_id, rid, now, "device")
            return False
        ok = await speak_text_to_device(device_id, text, label=f"reminder {rid}")
        store = get_networked_reminder_store()
        if ok:
            store.mark_fired(device_id, rid, now)
            self._publish_fired(device_id, rid, text)
            await self._cleanup_fired_mirror(device_id, rid)
            if auto and self._burst.record(rid, now):
                store.set_enabled(device_id, rid, False)
                self._burst.reset(rid)
                _logger.error(
                    f"[reminder] 熔断: rid={rid} did={device_id} "
                    f"{BURST_WINDOW_SEC}s 内自动触发超过 {BURST_LIMIT} 次，已自动禁用"
                )
        else:
            # speak_text_to_device 内部已涵盖 TTS 合成与双路推流失败；
            # 此处统一记一次尝试，沿用 DEVICE_RETRY_GAP(15s) 重试门限。
            store.set_attempt(device_id, rid, now, "device")
        return ok

    def _publish_fired(self, device_id, rid, text):
        try:
            from fuguoai_core.utils.event_bus import get_event_bus
            get_event_bus().publish({
                "type": "reminder.fired",
                "device_id": device_id,
                "rid": rid,
                "text": text,
                "ts": int(time.time()),
            })
        except Exception:
            pass

    async def _cleanup_fired_mirror(self, device_id, rid):
        """once 提醒触发后清理设备侧日程镜像（2026-10-18）。

        语音提醒经 networked_reminder 创建时会把任务镜像进 schedule_mirror
        （create 时写入 rid）。once 触发后若不清镜像，设备侧会残留一条已触发
        的一次性日程——固件 ExpireOnceTasks 只清昨天及更早的 once，当天的
        过点 once 会一直挂在日程屏上（用户 2026-10-18 实证"过时间了也没
        清除"）。daily/weekly 不清理（周期性日程继续有效）。

        best-effort：任何异常只记 warning，绝不影响 fire_one 主流程与
        播放结果；设备离线时 push 失败由重连 pull 的 stale deletes 差集兜底。
        """
        try:
            item = get_networked_reminder_store().get(device_id, rid)
            if not item:
                return
            if (item.get("schedule") or {}).get("type") != "once":
                return
            from fuguoai_core.utils.schedule_mirror_store import get_schedule_mirror_store
            from fuguoai_core.utils.schedule_downlink import push_schedule_to_device
            store = get_schedule_mirror_store()
            iid = store.delete_cloud_task_by_rid(device_id, rid)
            if iid is None:
                return  # 无 rid 镜像条目（legacy 存量），靠 pull 差集清理
            pushed = await push_schedule_to_device(device_id, tasks=[], deletes=[iid])
            # 双路日志：模块级 _logger 的 INFO 被根 WARNING 压制看不到（同
            # _speak_via_chat 的已知问题），关键清理动作必须走 conn.logger。
            msg = (
                f"[reminder] once 触发后镜像清理 rid={rid} did={device_id} "
                f"cloud_id={iid} pushed={pushed}"
            )
            conn = _get_conn(device_id)
            if conn is not None:
                conn.logger.bind(tag="reminder").info(msg)
            else:
                _logger.info(msg)
        except Exception as e:
            _logger.warning(f"[reminder] once 镜像清理失败(忽略) rid={rid} did={device_id}: {e}")

    async def _worker(self):
        store = get_networked_reminder_store()
        while True:
            try:
                await asyncio.sleep(WORKER_INTERVAL)
                now = int(time.time())
                for it in store.pending_due(now):
                    did = it["device_id"]
                    if did in self._playing:
                        continue
                    self._playing.add(did)
                    try:
                        await self.fire_one(did, it["rid"], it["text"], auto=True)
                    except Exception as e:
                        _logger.warning(f"[reminder] fire_one 异常(继续): {e}")
                    finally:
                        self._playing.discard(did)
            except asyncio.CancelledError:
                return
            except Exception as e:
                _logger.warning(f"[reminder] worker 异常(继续): {e}")

    def ensure_worker(self):
        if self._task is None or self._task.done():
            try:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = asyncio.get_event_loop()
                self._task = loop.create_task(self._worker())
            except Exception:
                self._task = None


_SCHEDULER = None


def get_networked_reminder_scheduler():
    global _SCHEDULER
    if _SCHEDULER is None:
        _SCHEDULER = NetworkedReminderScheduler()
    return _SCHEDULER
