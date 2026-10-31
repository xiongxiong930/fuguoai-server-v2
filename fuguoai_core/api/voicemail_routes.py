"""离线语音留言 REST（docs/VOICEMAIL_DESIGN.md §6，全部新增路由）。

前缀 /api/v1/voicemail，api_guard + 设备归属校验（与 intercom_routes 同款）。

- POST   /api/v1/voicemail                 留言上传（multipart: meta + audio）
- GET    /api/v1/voicemail/list            留言列表（?device_id=&status=&limit=&offset=）
- GET    /api/v1/voicemail/{msg_id}/audio  拉音频（.fgvm→wav 现转，?device_id=）
- POST   /api/v1/voicemail/{msg_id}/read   标记已读（body: {device_id}）
- DELETE /api/v1/voicemail/{msg_id}        删除（?device_id=）

投递 worker 随 app 启动（on_startup），设备上线+空闲即自动播报。
"""

import io
import json
import logging
import struct

from aiohttp import web

from fuguoai_core.api.registry import register_routes
from fuguoai_core.guards import api_guard
from fuguoai_core.device_access import DeviceBindingStore
from fuguoai_core.utils.util import normalize_device_id
from fuguoai_core.utils.voicemail_store import (
    get_voicemail_store, read_fgvm, transcode_upload,
    MAX_UPLOAD_BYTES, SAMPLE_RATE,
)

TAG = "voicemail_routes"
logger = logging.getLogger(TAG)


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
    return (user or {}).get("user_id") or (user or {}).get("openid")


def _get_owned_device(uid: str, raw_device_id: str):
    """归一化归属匹配（与 intercom_routes 同款；非本人/无匹配返回 None）。"""
    did = normalize_device_id(raw_device_id)
    for d in DeviceBindingStore().list_all():
        if normalize_device_id(d.get("device_id", "")) == did:
            return d if d.get("user_id") == uid else None
    return None


@register_routes
def register(app):
    app.router.add_post("/api/v1/voicemail", handle_upload)
    app.router.add_get("/api/v1/voicemail/list", handle_list)
    app.router.add_get("/api/v1/voicemail/{msg_id}/audio", handle_audio)
    app.router.add_post("/api/v1/voicemail/{msg_id}/read", handle_read)
    app.router.add_delete("/api/v1/voicemail/{msg_id}", handle_delete)
    for p in ("/api/v1/voicemail", "/api/v1/voicemail/list",
              "/api/v1/voicemail/{msg_id}/audio",
              "/api/v1/voicemail/{msg_id}/read", "/api/v1/voicemail/{msg_id}"):
        app.router.add_options(p, handle_options)
    app.on_startup.append(_start_worker)


async def _start_worker(app):
    """app 启动后拉起投递 worker（loop 此时必然在跑）。"""
    try:
        from fuguoai_core.utils.voicemail_player import get_voicemail_player
        get_voicemail_player().ensure_worker()
        logger.info("[voicemail] 投递 worker 已启动")
    except Exception as e:
        logger.warning(f"[voicemail] worker 启动失败: {e}")


async def handle_options(request):
    return _add_cors(web.Response(text=""))


async def handle_upload(request):
    """POST /api/v1/voicemail — multipart: meta(json) + audio(binary)。"""
    user = api_guard(request)
    if isinstance(user, web.Response):
        return _add_cors(user)
    uid = _user_id(user)

    meta, audio = {}, b""
    try:
        reader = await request.multipart()
        async for part in reader:
            if part.name == "meta":
                meta = json.loads((await part.read()).decode("utf-8"))
            elif part.name == "audio":
                audio = await part.read()
                if len(audio) > MAX_UPLOAD_BYTES:
                    return _add_cors(_err(413, "too_large", "音频超过 2MB"))
    except Exception:
        return _add_cors(_err(400, "bad_request", "需 multipart: meta(json)+audio(binary)"))

    target_raw = str((meta or {}).get("target_device_id") or "").strip()
    source_fmt = str((meta or {}).get("source_fmt") or "pcm").strip().lower()
    if not target_raw or not audio:
        return _add_cors(_err(400, "bad_request", "缺少 target_device_id 或 audio"))
    target_dev = _get_owned_device(uid, target_raw)
    if not target_dev:
        return _add_cors(_err(404, "not_found", "target_device_id 未找到或无权限"))

    try:
        frames = transcode_upload(audio, source_fmt)
    except ValueError as e:
        code = str(e)
        if code in ("too_long", "too_short"):
            return _add_cors(_err(400, code,
                                  "留言时长须在 0.2s ~ 60s 之间"))
        return _add_cors(_err(400, "bad_audio", code))
    except Exception as e:
        logger.warning(f"[voicemail] 转码失败 fmt={source_fmt}: {e}")
        return _add_cors(_err(400, "bad_audio", "音频解码失败"))

    duration_ms = len(frames) * 20
    store = get_voicemail_store()
    row = store.add(target_dev["device_id"], "phone-" + str(uid), uid,
                    frames, duration_ms, source_fmt)
    if row.get("error"):
        return _add_cors(_err(409, row["error"]))
    # 设备在线且空闲则即刻尝试投递（不等 worker 下一轮）
    try:
        from fuguoai_core.utils.voicemail_player import get_voicemail_player, _device_idle
        player = get_voicemail_player()
        player.ensure_worker()
        if _device_idle(target_dev["device_id"]):
            import asyncio
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.get_event_loop()
            loop.create_task(player.deliver_for_device(target_dev["device_id"]))
    except Exception:
        pass
    logger.info(f"[voicemail] 新留言 {row['msg_id']} -> {target_dev['device_id']} "
                f"{duration_ms}ms fmt={source_fmt}")
    return _add_cors(web.json_response(
        {"ok": True, "data": {"msg_id": row["msg_id"], "duration_ms": duration_ms}},
        dumps=_dumps))


async def handle_list(request):
    user = api_guard(request)
    if isinstance(user, web.Response):
        return _add_cors(user)
    uid = _user_id(user)
    device_raw = request.query.get("device_id", "").strip()
    if not device_raw:
        return _add_cors(_err(400, "bad_request", "缺少 device_id"))
    dev = _get_owned_device(uid, device_raw)
    if not dev:
        return _add_cors(_err(404, "not_found", "device_id 未找到或无权限"))
    try:
        limit = int(request.query.get("limit", "20"))
        offset = int(request.query.get("offset", "0"))
    except Exception:
        limit, offset = 20, 0
    items = get_voicemail_store().list(
        dev["device_id"], request.query.get("status") or None, limit, offset)
    return _add_cors(web.json_response({"ok": True, "data": items}, dumps=_dumps))


def _resolve_msg(request):
    """公共取参与归属校验 → (uid, device_id, msg_id) 或 web.Response。"""
    user = api_guard(request)
    if isinstance(user, web.Response):
        return user
    uid = _user_id(user)
    device_raw = request.query.get("device_id", "").strip()
    if not device_raw:
        return _err(400, "bad_request", "缺少 device_id")
    dev = _get_owned_device(uid, device_raw)
    if not dev:
        return _err(404, "not_found", "device_id 未找到或无权限")
    return uid, dev["device_id"], request.match_info["msg_id"]


async def handle_audio(request):
    """GET /{msg_id}/audio — .fgvm→wav 现转（小程序 InnerAudioContext 直接播）。"""
    r = _resolve_msg(request)
    if isinstance(r, web.Response):
        return _add_cors(r)
    _, device_id, msg_id = r
    store = get_voicemail_store()
    if not store.get(device_id, msg_id):
        return _add_cors(_err(404, "not_found", "留言不存在"))
    try:
        _, frames = read_fgvm(store.audio_path(device_id, msg_id))
        import opuslib_next
        dec = opuslib_next.Decoder(SAMPLE_RATE, 1)
        pcm = bytearray()
        for fr in frames:
            try:
                pcm += dec.decode(fr, 320)
            except Exception:
                pcm += b"\x00" * 640  # 坏帧补静音，不中断
        wav = _wav_wrap(bytes(pcm))
    except Exception as e:
        logger.warning(f"[voicemail] 音频转出失败 {msg_id}: {e}")
        return _add_cors(_err(500, "internal", "音频转出失败"))
    resp = web.Response(body=wav, content_type="audio/wav")
    resp.headers["Content-Disposition"] = f'inline; filename="{msg_id}.wav"'
    return _add_cors(resp)


def _wav_wrap(pcm: bytes) -> bytes:
    """16k/mono/s16le PCM → WAV 字节流。"""
    buf = io.BytesIO()
    buf.write(b"RIFF")
    buf.write(struct.pack("<I", 36 + len(pcm)))
    buf.write(b"WAVEfmt ")
    buf.write(struct.pack("<IHHIIHH", 16, 1, 1, SAMPLE_RATE,
                          SAMPLE_RATE * 2, 2, 16))
    buf.write(b"data")
    buf.write(struct.pack("<I", len(pcm)))
    buf.write(pcm)
    return buf.getvalue()


async def handle_read(request):
    r = _resolve_msg(request)
    if isinstance(r, web.Response):
        return _add_cors(r)
    _, device_id, msg_id = r
    ok = get_voicemail_store().set_status(device_id, msg_id, "read")
    if not ok:
        return _add_cors(_err(404, "not_found", "留言不存在"))
    return _add_cors(web.json_response({"ok": True}, dumps=_dumps))


async def handle_delete(request):
    r = _resolve_msg(request)
    if isinstance(r, web.Response):
        return _add_cors(r)
    _, device_id, msg_id = r
    get_voicemail_store().delete(device_id, msg_id)
    return _add_cors(web.json_response({"ok": True}, dumps=_dumps))
