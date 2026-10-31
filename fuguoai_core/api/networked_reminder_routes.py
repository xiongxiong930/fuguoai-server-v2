"""联网定时提醒 REST（新增路由，HTTP 只扩不删改）。

前缀 /api/v1/devices/{device_id}/reminders/networked
- POST   .../networked                 创建联网提醒 {text, schedule}
- GET    .../networked                 列表(?include_disabled=0|1)
- DELETE .../networked/{rid}          取消
- POST   .../networked/{rid}/fire     立即触发(测试 / "现在提醒我")

调度 worker 随 app 启动(on_startup)。
事件：reminder.fired 经既有 /api/v1/events WS 推送；小程序加 case 即可。
"""

import json
import logging

from aiohttp import web

from fuguoai_core.api.registry import register_routes
from fuguoai_core.guards import api_guard
from fuguoai_core.device_access import DeviceBindingStore
from fuguoai_core.utils.util import normalize_device_id
from fuguoai_core.utils.networked_reminder_store import get_networked_reminder_store

TAG = "networked_reminder_routes"
logger = logging.getLogger(TAG)


def _dumps(o):
    return json.dumps(o, ensure_ascii=False)


def _add_cors(request, resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "content-type, authorization"
    return resp


def _err(status, error, message=None):
    body = {"ok": False, "error": error}
    if message:
        body["message"] = message
    return web.json_response(body, status=status, dumps=_dumps)


def _user_id(user):
    return (user or {}).get("user_id") or (user or {}).get("openid")


def _get_owned_device(binding, uid, raw_device_id):
    did = normalize_device_id(raw_device_id)
    for d in binding.list_all():
        if normalize_device_id(d.get("device_id", "")) == did:
            if d.get("user_id") == uid:
                return d
            return None  # 命中但非本人，按无权限处理
    return None


@register_routes
def register(app):
    app.router.add_post("/api/v1/devices/{device_id}/reminders/networked", handle_create)
    app.router.add_get("/api/v1/devices/{device_id}/reminders/networked", handle_list)
    app.router.add_post("/api/v1/devices/{device_id}/reminders/networked/{rid}/fire", handle_fire)
    app.router.add_delete("/api/v1/devices/{device_id}/reminders/networked/{rid}", handle_delete)
    for p in ("/api/v1/devices/{device_id}/reminders/networked",
              "/api/v1/devices/{device_id}/reminders/networked/{rid}/fire",
              "/api/v1/devices/{device_id}/reminders/networked/{rid}"):
        app.router.add_options(p, handle_options)
    app.on_startup.append(_start_scheduler)


async def _start_scheduler(app):
    try:
        from fuguoai_core.utils.networked_reminder_scheduler import get_networked_reminder_scheduler
        get_networked_reminder_scheduler().ensure_worker()
        logger.info("[reminder] 调度 worker 已启动")
    except Exception as e:
        logger.warning(f"[reminder] 调度 worker 启动失败: {e}")


async def _require_device(request):
    """鉴权 + 设备归属校验；返回 (device_id, uid, err_resp)。"""
    user = api_guard(request)
    if isinstance(user, web.Response):
        return None, None, _add_cors(request, user)
    uid = _user_id(user)
    raw = request.match_info.get("device_id", "").strip()
    binding = DeviceBindingStore()
    owned = _get_owned_device(binding, uid, raw)
    if not owned:
        return None, None, _add_cors(request, _err(404, "未找到该设备或无权限"))
    return owned["device_id"], uid, None


async def handle_create(request):
    device_id, uid, err = await _require_device(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return _add_cors(request, _err(400, "invalid_json"))
    text = (body or {}).get("text")
    schedule = (body or {}).get("schedule")
    rid, e = get_networked_reminder_store().add(device_id, uid, text, schedule)
    if rid is None:
        return _add_cors(request, _err(400, "invalid", e))
    item = get_networked_reminder_store().get(device_id, rid)
    return _add_cors(request, web.json_response(
        {"ok": True, "data": {
            "rid": rid,
            "next_fire_at": item.get("next_fire_at"),
            "schedule": schedule,
            "text": (text or "").strip(),
        }},
        dumps=_dumps))


async def handle_list(request):
    device_id, uid, err = await _require_device(request)
    if err:
        return err
    inc = request.query.get("include_disabled", "0") == "1"
    items = get_networked_reminder_store().list(device_id, include_disabled=inc)
    return _add_cors(request, web.json_response(
        {"ok": True, "data": {"device_id": device_id, "count": len(items), "reminders": items}},
        dumps=_dumps))


async def handle_delete(request):
    device_id, uid, err = await _require_device(request)
    if err:
        return err
    rid = request.match_info.get("rid", "").strip()
    item = get_networked_reminder_store().get(device_id, rid)
    if not item:
        return _add_cors(request, _err(404, "not_found"))
    ok = get_networked_reminder_store().cancel(device_id, rid)
    return _add_cors(request, web.json_response({"ok": True, "removed": ok}, dumps=_dumps))


async def handle_fire(request):
    device_id, uid, err = await _require_device(request)
    if err:
        return err
    rid = request.match_info.get("rid", "").strip()
    item = get_networked_reminder_store().get(device_id, rid)
    # fired 的 once 是终态：拒绝手动补触（防连点重复播报，见 fire_count 虚高复盘）
    if not item or item.get("status") in ("disabled", "fired"):
        return _add_cors(request, _err(404, "not_found"))
    try:
        from fuguoai_core.utils.networked_reminder_scheduler import get_networked_reminder_scheduler
        ok = await get_networked_reminder_scheduler().fire_one(device_id, rid, item["text"])
    except Exception as e:
        return _add_cors(request, _err(500, "fire_failed", str(e)))
    return _add_cors(request, web.json_response(
        {"ok": True, "fired": bool(ok), "rid": rid}, dumps=_dumps))


async def handle_options(request):
    return _add_cors(request, web.Response(status=204))
