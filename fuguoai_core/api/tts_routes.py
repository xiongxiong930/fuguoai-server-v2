"""天使心小程序 app 面 TTS 音频端点（新增路由，HTTP 只扩不删改）。

背景（见《天使心·TTS对接说明》）：微信小程序运行时无 TTS 引擎、也无 Web Speech API，
点 AI 气泡试听统一走「请求 /tts?text=...&voice=... → 拿 MP3 → 播放」。fuguoai-server
此前没有这个 app 面文字→音频 HTTP 端点（仅 voicemail 回放是音频路由），故新增。

端点（与对接说明 §6 对齐，前缀 /api/v1 与 registry 约定一致）：
- GET /api/v1/tts?text=...&voice=zh-CN-XiaoxiaoNeural   返回 audio/mpeg（MP3）
- GET /api/v1/voices                                      返回音色列表 JSON（供前端音色选择）

实现要点
--------
- 主用 provider **固定为腾讯云 TTS**（不随 config ``selected_module.TTS`` 切换），
  因为 159 的 selected_module.TTS 仍是 EdgeTTS（全局设备对话路径），微软收口 403 后
  该路径不可用；本端点独立切腾讯云，避免波及设备/对讲语音。腾讯云不可用才回退
  selected_module.TTS。
- EdgeTTS 降级为兜底：主用 provider 合成失败时（鉴权/网络抖动）回退到 EdgeTTS，
  避免整端 502 静音；主用本身就是 EdgeTTS 时不重复兜底。
- 微软 EdgeTTS 收口 403 后不再作为唯一路径；兜底仍保留以兼容临时抖动与
  selected_module.TTS=EdgeTTS 的回退配置。
- per-voice 缓存 TTSProvider 实例（角色音色就几种），避免每请求重建；delete_audio_file
  传 None（本端点只回字节、不落盘，无文件需清理）。
- 鉴权：按《天使心·TTS对接说明》§6，本端点鉴权为**可选**——客户小程序 speakWithVoice
  当前不带 JWT（直连本地 8765 无鉴权服务），故**开放**以让 TTS_BASE_URL 切到
  https://<your-domain>/api/v1 即可出声、不被挡。生产若要收口，加回 core.guards.api_guard
  + 限流即可（前端 §6 step 3 已预见补 Authorization 头）。
- CORS 放行 * 以兼容浏览器预览（预览.html / 小程序 DevTools）。
- 合成失败统一降级：超时→504，其他→502，绝不把异常抛给调用方。
"""

import logging
import threading

from aiohttp import web

from fuguoai_core.api.registry import register_routes
from fuguoai_core.utils.config_holder import get_config
from fuguoai_core.utils.tts import create_instance

TAG = "tts_routes"
logger = logging.getLogger(TAG)

_DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

# 常用中文 EdgeTTS 音色（供 /voices；完整列表见微软文档）。
_KNOWN_VOICES = [
    {"id": "zh-CN-XiaoxiaoNeural", "label": "晓晓（温柔女声）", "gender": "female"},
    {"id": "zh-CN-YunxiNeural", "label": "云希（朝气男声）", "gender": "male"},
    {"id": "zh-CN-YunyangNeural", "label": "云扬（新闻男声）", "gender": "male"},
    {"id": "zh-CN-XiaoyiNeural", "label": "晓伊（温柔女声）", "gender": "female"},
    {"id": "zh-CN-YunjianNeural", "label": "云健（浑厚男声）", "gender": "male"},
    {"id": "zh-CN-XiaochenNeural", "label": "晓辰（活泼女声）", "gender": "female"},
]


# ---------------------------------------------------------------------------
# 响应封装（与 character_routes 同范式：CORS * + {ok,msg} 错误体）
# ---------------------------------------------------------------------------
def _add_cors(request, resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "content-type, authorization"
    return resp


def _err(status, message):
    return web.json_response({"ok": False, "msg": message}, status=status)


# ---------------------------------------------------------------------------
# 主用 / 兜底 TTS provider（按 config selected_module.TTS 选择 + EdgeTTS 兜底）
# ---------------------------------------------------------------------------
# None=未初始化；_PRIMARY_FAILED=初始化失败（后续请求直接走兜底/502）
_PRIMARY = None
_PRIMARY_FAILED = object()
_PRIMARY_LOCK = threading.Lock()

# EdgeTTS 兜底 provider 缓存（per-voice 单例）
_FALLBACK_CACHE = {}
_FALLBACK_LOCK = threading.Lock()

# 腾讯云主用 provider 的 per-voice 缓存（角色音色就几种，避免每请求重建）。
# 与 _FALLBACK_CACHE(EdgeTTS) 同范式：按请求的 voice 构建/复用腾讯 provider，
# 把请求声线透传进 private_voice，使 /tts?voice=<角色音色> 真正生效
# （此前 _get_primary 单例只用 config 固定声线，5 角色全同声）。
_TENCENT_CACHE = {}
_TENCENT_LOCK = threading.Lock()


def _build_provider(name):
    """按 config.TTS 下的段名构建 provider 实例；段不存在返回 None。"""
    cfg = get_config() or {}
    tts_sec = cfg.get("TTS", {}) or {}
    block = tts_sec.get(name)
    if not block:
        return None
    # 端点统一以 audio/mpeg(MP3) 返回；腾讯云默认 format=wav 会导致字节是 WAV、
    # 响应头却是 audio/mpeg，小程序播放器可能解码失败。故主用腾讯云强制 mp3，
    # 与 EdgeTTS 兜底输出保持一致（仅作用于本端点，不改全局 config，无 blast radius）。
    if name == "TencentTTS":
        block = dict(block)
        block["format"] = "mp3"
    tts_type = name if "type" not in block else block["type"]
    return create_instance(tts_type, block, None)


def _get_primary():
    """天使心 app 面 TTS 主用 = 腾讯云 TTS。

    微软 EdgeTTS 收口 403 后整条链路挂掉，故本端点主用腾讯云 TTS；
    159 的 selected_module.TTS 仍为 EdgeTTS（全局设备对话路径），不随之切换，
    以免波及设备/对讲语音。腾讯云不可用才回退 selected_module.TTS。
    """
    global _PRIMARY
    if _PRIMARY is not None:
        return None if _PRIMARY is _PRIMARY_FAILED else _PRIMARY
    with _PRIMARY_LOCK:
        if _PRIMARY is not None:
            return None if _PRIMARY is _PRIMARY_FAILED else _PRIMARY
        try:
            _PRIMARY = _build_provider("TencentTTS")
            if _PRIMARY is not None:
                logger.info("[tts] 主用 TTS provider 就绪: TencentTTS")
            else:
                # 未配置腾讯云 TTS：回退到 selected_module.TTS（保持原行为）
                sel = (get_config() or {}).get("selected_module", {}).get("TTS")
                logger.warning("[tts] 未配置 TencentTTS，回退 selected_module.TTS=%s", sel)
                _PRIMARY = _build_provider(sel) if sel else None
        except Exception as e:
            logger.warning("[tts] 主用 TTS provider 初始化失败，转兜底: %s", e)
            _PRIMARY = _PRIMARY_FAILED
    return None if _PRIMARY is _PRIMARY_FAILED else _PRIMARY


def _get_fallback(voice):
    """EdgeTTS 兜底 provider（按 voice 缓存）。

    主用腾讯云 TTS 合成失败时使用；EdgeTTS 微软收口 403 后通常也不可用，
    仅作为保底，失败则整端透出 502（诊断期带真实异常）。
    """
    with _FALLBACK_LOCK:
        p = _FALLBACK_CACHE.get(voice)
        if p is None:
            try:
                # 惰性 import：避免路由模块加载即拉起 base/edge 重链
                from fuguoai_core.providers.tts.edge import TTSProvider as EdgeTTSProvider
                p = EdgeTTSProvider({"voice": voice}, delete_audio_file=None)
                _FALLBACK_CACHE[voice] = p
            except Exception as e:
                logger.warning("[tts] EdgeTTS 兜底初始化失败: %s", e)
                _FALLBACK_CACHE[voice] = _PRIMARY_FAILED
                return None
        return None if p is _PRIMARY_FAILED else p


def _get_tencent(voice):
    """按请求的 voice 构建/复用腾讯云 provider（per-voice 缓存单例）。

    159 的 /tts 端点由小程序按角色 voice 透传音色；此前 _get_primary 单例只用
    config 固定声线 → 5 角色全同声。此处把请求 voice 透传进 private_voice
    （tencent._resolve_voice 读 private_voice），数字串直接当 VoiceType，非数字
    （遗留 Azure 串）回落 config 默认声线。voice 为空/默认时回退 _get_primary。
    """
    if not voice:
        return _get_primary()
    with _TENCENT_LOCK:
        p = _TENCENT_CACHE.get(voice)
        if p is not None:
            return None if p is _PRIMARY_FAILED else p
        try:
            cfg = get_config() or {}
            block = dict((cfg.get("TTS", {}) or {}).get("TencentTTS") or {})
            if not block:
                _TENCENT_CACHE[voice] = _PRIMARY_FAILED
                return _get_primary()
            block["format"] = "mp3"          # 与 _build_provider 一致：强制 mp3
            block["private_voice"] = voice    # 关键：透传请求声线
            p = create_instance("tencent", block, None)
            _TENCENT_CACHE[voice] = p
        except Exception as e:
            logger.warning("[tts] 腾讯 per-voice provider 构建失败 voice=%s: %s", voice, e)
            _TENCENT_CACHE[voice] = _PRIMARY_FAILED
            return None
    return p


# ---------------------------------------------------------------------------
# 路由处理
# ---------------------------------------------------------------------------
async def handle_tts(request):
    text = (request.query.get("text") or "").strip()
    voice = (request.query.get("voice") or _DEFAULT_VOICE).strip()
    # 角色出厂语速（spec 附录 A ±0.5，活字匣 profile.speed 透传；与 voice 同模式）。
    # 不传/空 → None → provider 用实例默认（config/ttsRate 链），**现有行为零变化**。
    speed = (request.query.get("speed") or "").strip() or None
    if not text:
        return _add_cors(request, _err(400, "missing text"))
    mp3 = None
    last_err = None
    # 1) 主用 provider（按请求 voice 透传，默认 TencentTTS）
    prov = _get_tencent(voice)
    if prov is not None:
        try:
            # 仅当请求显式带 speed 才传 kwarg；不传时保持旧签名调用（provider 用实例默认）
            kw = {"speed": speed} if speed is not None else {}
            mp3 = await prov.text_to_speak(text, None, **kw)
        except Exception as e:
            last_err = e
            logger.warning(f"[tts] 主用合成失败 voice={voice}: {e}")
    # 2) EdgeTTS 兜底（主用失败且主用非 EdgeTTS）
    if not mp3:
        fb = _get_fallback(voice)
        if fb is not None:
            try:
                mp3 = await fb.text_to_speak(text, None)
            except Exception as e:
                last_err = e
                logger.warning(f"[tts] 兜底合成失败 voice={voice}: {e}")
    if not mp3:
        msg = str(last_err) if last_err else "TTS 空音频"
        status = 504 if "超时" in msg else 502
        logger.warning(f"[tts] 合成彻底失败 voice={voice}: {msg}")
        # 生产：不向客户端泄露内部异常细节，仅返回通用错误（根因见服务端日志）
        return _add_cors(request, _err(status, "TTS 合成失败，请稍后重试"))
    resp = web.Response(body=mp3, content_type="audio/mpeg")
    resp.headers["Content-Disposition"] = 'inline; filename="tts.mp3"'
    return _add_cors(request, resp)


async def handle_voices(request):
    return _add_cors(request, web.json_response({"ok": True, "voices": _KNOWN_VOICES}))


async def handle_options(request):
    return _add_cors(request, web.Response(status=204))


@register_routes
def register(app):
    app.router.add_get("/api/v1/tts", handle_tts)
    app.router.add_get("/api/v1/voices", handle_voices)
    app.router.add_options("/api/v1/tts", handle_options)
    app.router.add_options("/api/v1/voices", handle_options)
