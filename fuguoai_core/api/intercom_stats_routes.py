"""对讲通话质量指标 REST（docs/INTERCOM_STATS_DESIGN.md v0.1）。

零侵入：@register_routes 自动发现挂载，数据全部来自 intercom_stats 内存 Hub。

- GET /api/v1/intercom/stats          活跃会话实时指标（无则 []）
- GET /api/v1/intercom/stats/recent   最近 N 通终态快照（?limit=20）

鉴权双口径：
- admin：Authorization Bearer == FUGUOAI_ADMIN_TOKEN → 看全部会话。
- 用户：api_guard 通过 → 只看与自己名下设备（或 phone-<uid>）相关的会话。
"""

import json
import logging
import os

from aiohttp import web

from fuguoai_core.api.registry import register_routes
from fuguoai_core.guards import api_guard
from fuguoai_core.device_access import DeviceBindingStore
from fuguoai_core.utils.intercom_stats import get_stats_hub

TAG = "intercom_stats_routes"
logger = logging.getLogger(TAG)


def _dumps(o):
    return json.dumps(o, ensure_ascii=False)


def _add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "content-type, authorization"
    return resp


def _is_admin(request) -> bool:
    token = (os.environ.get("FUGUOAI_ADMIN_TOKEN") or "").strip()
    if not token:
        return False
    auth = request.headers.get("Authorization", "")
    return auth.startswith("Bearer ") and auth[len("Bearer "):].strip() == token


def _user_devices(uid: str) -> set:
    ids = {"phone-" + str(uid)}
    try:
        for d in DeviceBindingStore().list_all():
            if d.get("user_id") == uid:
                ids.add(d.get("device_id"))
    except Exception:
        pass
    return ids


def _authorize(request):
    """返回 (ok, device_filter)。admin → (True, None=不过滤)；用户 → (True, 设备集合)。"""
    if _is_admin(request):
        return True, None
    user = api_guard(request)
    if isinstance(user, web.Response):
        return user, None
    uid = (user or {}).get("user_id") or (user or {}).get("openid")
    return True, _user_devices(uid)


def _filter(items: list, allow: set) -> list:
    if allow is None:
        return items
    return [it for it in items if it.get("a") in allow or it.get("b") in allow]


@register_routes
def register(app):
    app.router.add_get("/api/v1/intercom/stats", handle_stats)
    app.router.add_get("/api/v1/intercom/stats/recent", handle_stats_recent)
    app.router.add_options("/api/v1/intercom/stats", handle_options)
    app.router.add_options("/api/v1/intercom/stats/recent", handle_options)


async def handle_options(request):
    return _add_cors(web.Response(text=""))


async def handle_stats(request):
    ok, allow = _authorize(request)
    if isinstance(ok, web.Response):
        return _add_cors(ok)
    data = _filter(get_stats_hub().active(), allow)
    return _add_cors(web.json_response({"ok": True, "data": data}, dumps=_dumps))


async def handle_stats_recent(request):
    ok, allow = _authorize(request)
    if isinstance(ok, web.Response):
        return _add_cors(ok)
    try:
        limit = int(request.query.get("limit", "20"))
    except Exception:
        limit = 20
    data = _filter(get_stats_hub().recent(limit), allow)
    return _add_cors(web.json_response({"ok": True, "data": data}, dumps=_dumps))
