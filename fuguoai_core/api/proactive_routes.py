"""主动对话引擎 REST（新增路由，HTTP 只扩不删改）。

前缀 /api/v1/devices/{device_id}/proactive
- GET    .../proactive                 读取配置（开关/密度/静默时段/触发源）
- PUT    .../proactive                 更新配置
- POST   .../proactive/trigger         立即触发（调试 / "现在跟我聊"），可带 reason/text 覆盖

worker 随 app 启动(on_startup)：订阅 reminder.fired + 起扫描。
事件：proactive.said 经既有 /api/v1/events WS 推送；小程序加 case 即可。

红线合规：全新增路径；不碰 8000 WS；实时推送走 event_bus；下发零固件改动。
"""

import json
import logging

from aiohttp import web

from fuguoai_core.api.registry import register_routes
from fuguoai_core.guards import api_guard
from fuguoai_core.device_access import DeviceBindingStore
from fuguoai_core.utils.util import normalize_device_id
from fuguoai_core.utils.proactive_engine import (
    get_proactive_engine, Candidate, ProactiveStore)

TAG = "proactive_routes"
logger = logging.getLogger(TAG)


def _dumps(o):
    return json.dumps(o, ensure_ascii=False)


def _add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, PUT, POST, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "content-type, authorization"
    return resp


def _err(status, error, message=None):
    body = {"ok": False, "error": error}
    if message:
        body["message"] = message
    return _add_cors(web.json_response(body, status=status, dumps=_dumps))


def _user_id(user):
    return (user or {}).get("user_id") or (user or {}).get("openid")


def _get_owned_device(binding, uid, raw_device_id):
    did = normalize_device_id(raw_device_id)
    for d in binding.list_all():
        if normalize_device_id(d.get("device_id", "")) == did:
            if d.get("user_id") == uid:
                return d
            return None
    return None


@register_routes
def register(app):
    app.router.add_get("/api/v1/devices/{device_id}/proactive", handle_get)
    app.router.add_put("/api/v1/devices/{device_id}/proactive", handle_put)
    app.router.add_post("/api/v1/devices/{device_id}/proactive/trigger", handle_trigger)
    for p in ("/api/v1/devices/{device_id}/proactive",
              "/api/v1/devices/{device_id}/proactive/trigger"):
        app.router.add_options(p, handle_options)
    app.on_startup.append(_start_engine)


async def _start_engine(app):
    try:
        get_proactive_engine().start()
        logger.info("[proactive] 引擎已启动（订阅 reminder.fired + 扫描 worker）")
    except Exception as e:
        logger.warning(f"[proactive] 引擎启动失败: {e}")


async def _require_device(request):
    """鉴权 + 设备归属；返回 (device_id, err_resp)。"""
    user = api_guard(request)
    if isinstance(user, web.Response):
        return None, _add_cors(user)
    uid = _user_id(user)
    raw = request.match_info.get("device_id", "").strip()
    binding = DeviceBindingStore()
    owned = _get_owned_device(binding, uid, raw)
    if not owned:
        return None, _err(404, "未找到该设备或无权限")
    return owned["device_id"], None


async def handle_get(request):
    device_id, err = await _require_device(request)
    if err:
        return err
    cfg = get_proactive_engine().get_config(device_id)
    return _add_cors(web.json_response({"ok": True, "data": cfg}, dumps=_dumps))


async def handle_put(request):
    device_id, err = await _require_device(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return _err(400, "invalid_json")
    if not isinstance(body, dict):
        return _err(400, "invalid_body")
    cfg = get_proactive_engine().set_config(device_id, body)
    return _add_cors(web.json_response({"ok": True, "data": cfg}, dumps=_dumps))


async def handle_trigger(request):
    device_id, err = await _require_device(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = (body or {}).get("reason") or "manual"
    text = (body or {}).get("text")
    if text:
        cand = Candidate(reason=reason, topic=f"manual:{reason}",
                         score=1.0, text=text)
    else:
        # 无文本覆盖：走引擎评估（取当前最优候选），若无非手动候选则用通用兜底
        eng = get_proactive_engine()
        cands = eng.evaluate(device_id)
        cand = eng.arbitrate(cands) or Candidate(
            reason="manual", topic="manual", score=1.0,
            text="想和你聊会儿，最近怎么样？")
    try:
        ok = await eng.fire(device_id, cand)
    except Exception as e:
        return _err(500, "fire_failed", str(e))
    return _add_cors(web.json_response(
        {"ok": True, "fired": bool(ok), "trigger": cand.reason}, dumps=_dumps))


async def handle_options(request):
    return _add_cors(web.Response(status=204))
