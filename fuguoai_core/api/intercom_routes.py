"""对讲呼叫编排 REST（契约 v1.8r1 §5，B-3）。

零侵入接入：由 fuguoai_core.api.registry 自动发现挂载。低层调试接口
POST /api/v1/devices/{id}/intercom 原样保留（§5.5），小程序一律走本模块。

接口（前缀 /api/v1/intercom，用户态守卫）
- POST /api/v1/intercom/call         发起呼叫 {my_device_id, target_device_id}
                                     → {call_id, status:"calling"|"connected"}
                                     409: peer_busy / self_call；M4 前仅同账号
- POST /api/v1/intercom/call/end     挂断 {call_id}
- GET  /api/v1/intercom/call/active  当前活跃呼叫（无则 data:null）

状态机（§5.4）：calling → connected(established) / ended(peer_left|hangup)
                / failed(peer_offline|peer_busy)
离线呼叫不拒绝：进 pending（TTL 30s，B-2 上线补偿），超时 failed(peer_offline)。
"""

import json
import logging

from aiohttp import web

from fuguoai_core.api.registry import register_routes
from fuguoai_core.guards import api_guard
from fuguoai_core.device_access import DeviceBindingStore
from fuguoai_core.utils.util import normalize_device_id
from fuguoai_core.utils.intercom_call_store import get_call_orchestrator

TAG = "intercom_routes"
logger = logging.getLogger(TAG)


def _dumps(o):
    return json.dumps(o, ensure_ascii=False)


def _add_cors(request, resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "content-type, authorization"
    return resp


def _err(status, error, message=None):
    body = {"ok": False, "error": error}
    if message:
        body["message"] = message
    return web.json_response(body, status=status, dumps=_dumps)


def _user_id(user: dict) -> str:
    return (user or {}).get("user_id") or (user or {}).get("openid")


def _get_owned_device(binding, uid: str, raw_device_id: str):
    """与 devices_routes 同款归一化归属匹配（非本人/无匹配返回 None）。"""
    did = normalize_device_id(raw_device_id)
    for d in binding.list_all():
        if normalize_device_id(d.get("device_id", "")) == did:
            if d.get("user_id") == uid:
                return d
            return None
    return None


@register_routes
def register(app):
    app.router.add_post("/api/v1/intercom/call", handle_call)
    app.router.add_post("/api/v1/intercom/call/end", handle_call_end)
    app.router.add_get("/api/v1/intercom/call/active", handle_call_active)
    app.router.add_options("/api/v1/intercom/call", handle_options)
    app.router.add_options("/api/v1/intercom/call/end", handle_options)
    app.router.add_options("/api/v1/intercom/call/active", handle_options)


async def handle_options(request):
    return _add_cors(request, web.Response(text=""))


async def handle_call(request):
    """POST /api/v1/intercom/call （契约 §5.1）"""
    user = api_guard(request)
    if isinstance(user, web.Response):
        return _add_cors(request, user)
    uid = _user_id(user)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    my_id = str((payload or {}).get("my_device_id") or "").strip()
    target_id = str((payload or {}).get("target_device_id") or "").strip()
    target_friend = str((payload or {}).get("target_friend_id") or "").strip()
    if not target_id:
        return _add_cors(request, _err(400, "bad_request", "需提供 target_device_id"))
    # M4 前仅同账号设备互呼（契约 §5.1 行为 2）
    if target_friend and target_friend != uid:
        return _add_cors(request, _err(403, "not_friend",
                                       "跨账号呼叫将在好友系统（M4）上线后开放"))

    binding = DeviceBindingStore()
    # B-5.2 模式B（契约 v1.9）：my_device_id 省略 / "phone" / "phone-*"
    # → 我方为手机端，身份由 JWT 派生 phone-<uid>（手机不自造 id，归属天然成立）。
    if not my_id or my_id.lower() == "phone" or my_id.startswith("phone-"):
        my_device_id = "phone-" + str(uid)
    else:
        my_dev = _get_owned_device(binding, uid, my_id)
        if not my_dev:
            return _add_cors(request, _err(404, "not_found", "my_device_id 未找到或无权限"))
        my_device_id = my_dev["device_id"]
    target_dev = _get_owned_device(binding, uid, target_id)
    if not target_dev:
        return _add_cors(request, _err(404, "not_found", "target_device_id 未找到或无权限"))

    orch = get_call_orchestrator()
    result = await orch.create_call(uid, my_device_id, target_dev["device_id"])
    if result and result.get("error"):
        code = result["error"]
        logger.info(f"[intercom.call] user={uid} {my_id}->{target_id} 拒绝: {code}")
        return _add_cors(request, _err(409, code))
    logger.info(
        f"[intercom.call] user={uid} {result['my_device_id']}->{result['target_device_id']} "
        f"call={result['call_id']} status={result['status']}"
    )
    return _add_cors(request, web.json_response(
        {"ok": True, "data": {"call_id": result["call_id"], "status": result["status"]}},
        dumps=_dumps,
    ))


async def handle_call_end(request):
    """POST /api/v1/intercom/call/end （契约 §5.2）"""
    user = api_guard(request)
    if isinstance(user, web.Response):
        return _add_cors(request, user)
    uid = _user_id(user)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    call_id = str((payload or {}).get("call_id") or "").strip()
    if not call_id:
        return _add_cors(request, _err(400, "bad_request", "缺少 call_id"))
    orch = get_call_orchestrator()
    result = await orch.end_call(uid, call_id)
    if result and result.get("error"):
        return _add_cors(request, _err(404, "not_found", "呼叫不存在或无权限"))
    logger.info(f"[intercom.call] end user={uid} call={call_id} -> {result['status']}")
    return _add_cors(request, web.json_response(
        {"ok": True, "data": {"call_id": call_id, "status": result["status"]}},
        dumps=_dumps,
    ))


async def handle_call_active(request):
    """GET /api/v1/intercom/call/active （契约 §5.3）"""
    user = api_guard(request)
    if isinstance(user, web.Response):
        return _add_cors(request, user)
    uid = _user_id(user)
    snap = get_call_orchestrator().active_for_user(uid)
    if not snap:
        return _add_cors(request, web.json_response(
            {"ok": True, "data": None}, dumps=_dumps
        ))
    # 附对端展示信息（device_name，契约 §5.3）
    data = {
        "call_id": snap["call_id"],
        "status": snap["status"],
        "my_device_id": snap["my_device_id"],
        "target_device_id": snap["target_device_id"],
        "started_at": snap["connected_at"] or snap["created_at"],
    }
    try:
        binding = DeviceBindingStore()
        for d in binding.list_all():
            if d.get("device_id") == snap["target_device_id"]:
                data["peer_device"] = d.get("device_name") or snap["target_device_id"]
                break
    except Exception:
        pass
    return _add_cors(request, web.json_response(
        {"ok": True, "data": data}, dumps=_dumps
    ))
