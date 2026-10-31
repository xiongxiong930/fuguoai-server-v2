"""声纹（说话人识别）用户态 REST —— 给小程序「家庭成员/声纹」页对接。

前缀 /api/v1/voiceprint，统一走 core.guards.api_guard（小程序 JWT，M16 注入验证器）。

- POST   /api/v1/voiceprint/enroll           注册一名家庭成员（称呼 + 16k wav）
- GET    /api/v1/voiceprint                  列出已注册成员
- DELETE /api/v1/voiceprint/{voiceprint_id}  删除一名成员

设计要点
--------
- 称呼（爸爸/妈妈/哥哥…）是用户在小程序录入的自由文本，经本接口带入声纹组，
  解决「设备端无键盘、采不到称呼」的缺口（设备只负责采声 + 识别）。
- 复用既有 voiceprint_provider（腾讯云说话人识别）：组、密钥、1:N 识别逻辑不变；
  本模块只把「注册/列表/删除」从 admin 独占开放为小程序用户态，并加 JWT 守卫。
- 音频契约：Base64(WAV)，16k / 单声道 / 16bit（VoiceFormat=1, SampleRate=16000），
  与 provider.enroll_speaker 一致。小程序 RecorderManager 设 sampleRate=16000 即可直出。
- 当前声纹组为服务端单组（config voiceprint.group_id），暂未做多家庭隔离；
  多租户隔离属后续迭代，本接口不改变该模型。

零侵入接入：由 fuguoai_core.api.registry 自动发现并挂载。
"""
import base64
import json
import logging

from aiohttp import web

from fuguoai_core.api.registry import register_routes
from fuguoai_core.guards import api_guard
from fuguoai_core.utils.config_holder import get_config
from fuguoai_core.utils.voiceprint_provider import get_voiceprint_provider

TAG = "voiceprint_routes"
logger = logging.getLogger(TAG)

# 与 voicemail 等同款上限，防超大 body
_MAX_WAV_BYTES = 2 * 1024 * 1024  # 2MB base64(wav) ≈ 47s@16k


def _dumps(o):
    return json.dumps(o, ensure_ascii=False)


def _add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "content-type, authorization"
    return resp


def _err(status, error, message=None):
    body = {"ok": False, "error": error}
    if message:
        body["message"] = message
    return web.json_response(body, status=status, dumps=_dumps)


def _user_id(user: dict) -> str:
    return (user or {}).get("user_id") or (user or {}).get("openid") or "unknown"


def _provider():
    return get_voiceprint_provider(get_config())


@register_routes
def register(app):
    app.router.add_post("/api/v1/voiceprint/enroll", handle_enroll)
    app.router.add_get("/api/v1/voiceprint", handle_list)
    app.router.add_delete("/api/v1/voiceprint/{voiceprint_id}", handle_delete)
    for p in ("/api/v1/voiceprint/enroll", "/api/v1/voiceprint",
              "/api/v1/voiceprint/{voiceprint_id}"):
        app.router.add_options(p, handle_options)


async def handle_options(request):
    return _add_cors(web.Response(text=""))


async def handle_enroll(request):
    """POST /api/v1/voiceprint/enroll — 注册一名家庭成员。

    body: {name: 称呼(1-32字), wav_base64: Base64(16k mono wav)}
    """
    user = api_guard(request)
    if isinstance(user, web.Response):
        return _add_cors(user)
    uid = _user_id(user)

    try:
        body = await request.json()
    except Exception:
        return _add_cors(_err(400, "bad_request", "JSON 解析失败"))

    name = (body.get("name") or "").strip()
    wav_b64 = body.get("wav_base64") or body.get("audio")
    if not name or not wav_b64:
        return _add_cors(_err(400, "bad_request", "需要 name 与 wav_base64"))

    try:
        wav = base64.b64decode(wav_b64)
    except Exception:
        return _add_cors(_err(400, "bad_audio", "wav_base64 解码失败"))
    if len(wav) > _MAX_WAV_BYTES:
        return _add_cors(_err(413, "too_large", "音频超过 2MB（建议 1-8s 人声）"))

    provider = _provider()
    if provider is None:
        return _add_cors(_err(400, "not_configured",
                              "声纹未配置（需 voiceprint.provider=tencent + 密钥 + group_id）"))

    result = await provider.enroll_speaker(name, wav)
    if not result.get("ok"):
        return _add_cors(_err(400, "enroll_failed", result.get("error", "注册失败")))
    logger.info(f"[voiceprint] 用户 {uid} 注册成员: {name} -> {result.get('voiceprint_id')}"
                f"{(' 克隆音色=' + result['voice_id']) if result.get('voice_id') else ' (音色克隆未启用/失败)'}")
    return _add_cors(web.json_response(
        {"ok": True, "data": {
            "voiceprint_id": result.get("voiceprint_id"),
            "name": result.get("name"),
            "voice_id": result.get("voice_id"),
        }}, dumps=_dumps))


async def handle_list(request):
    """GET /api/v1/voiceprint — 列出已注册成员。"""
    user = api_guard(request)
    if isinstance(user, web.Response):
        return _add_cors(user)

    provider = _provider()
    if provider is None:
        return _add_cors(_err(400, "not_configured", "声纹未配置"))
    return _add_cors(web.json_response(
        {"ok": True, "data": provider.list_speakers()}, dumps=_dumps))


async def handle_delete(request):
    """DELETE /api/v1/voiceprint/{voiceprint_id} — 删除一名成员。"""
    user = api_guard(request)
    if isinstance(user, web.Response):
        return _add_cors(user)
    uid = _user_id(user)

    vid = request.match_info.get("voiceprint_id", "").strip()
    if not vid:
        return _add_cors(_err(400, "bad_request", "缺少 voiceprint_id"))

    provider = _provider()
    if provider is None:
        return _add_cors(_err(400, "not_configured", "声纹未配置"))
    result = await provider.delete_speaker(vid)
    if not result.get("ok"):
        return _add_cors(_err(404, "not_found", result.get("error", "删除失败")))
    logger.info(f"[voiceprint] 用户 {uid} 删除成员: {vid}")
    return _add_cors(web.json_response({"ok": True}, dumps=_dumps))
