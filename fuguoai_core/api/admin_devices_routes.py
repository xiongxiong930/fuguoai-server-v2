"""运营管理端：设备总览 API（/api/v1/admin/devices）。

设计约束（见项目红线）：
- HTTP API 只扩不删改：本文件**新增** admin 专用路径，不改动任何既有路由。
- 仅 admin token（Authorization: Bearer $FUGUOAI_ADMIN_TOKEN）可访问；
  普通用户请求返回 403。
- 不触碰 8000 WS 协议、不改动 chat/TTS 路径。
- 实时在线态取自 connection_registry（与设备 WS 心跳同源），不引入新状态。

返回字段刻意精简：列表只给「总览」所需（在线/状态/归属/最近活跃），
单设备详情（用量/记忆/留言/指令）复用既有用户态接口，由前端用对应 token 拉取。
"""

import os
import time

from aiohttp import web

from fuguoai_core.api.registry import register_routes
from fuguoai_core.device_access import DeviceBindingStore
from fuguoai_core.utils.connection_registry import get_connection_registry


def _is_admin(request) -> bool:
    token = (os.environ.get("FUGUOAI_ADMIN_TOKEN") or "").strip()
    if not token:
        return False
    auth = request.headers.get("Authorization", "")
    return auth.startswith("Bearer ") and auth[len("Bearer "):].strip() == token


def _cors(resp: web.Response) -> web.Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    return resp


def _rel(ts: int):
    """把秒级时间戳转人类可读相对时间；None/0 返回空串。"""
    if not ts:
        return ""
    try:
        diff = int(time.time()) - int(ts)
        if diff < 0:
            diff = 0
        if diff < 60:
            return f"{diff}秒前"
        if diff < 3600:
            return f"{diff // 60}分钟前"
        if diff < 86400:
            return f"{diff // 3600}小时前"
        return f"{diff // 86400}天前"
    except Exception:
        return ""


async def handle_admin_devices(request: web.Request):
    """GET /api/v1/admin/devices —— 运营侧全部设备总览（admin only）。"""
    if not _is_admin(request):
        return _cors(web.json_response(
            {"ok": False, "error": "admin token required"}, status=403))
    try:
        reg = get_connection_registry()
        items = []
        for d in DeviceBindingStore().list_all():
            did = d.get("device_id")
            live = False
            try:
                live = bool(reg.is_online(did))
            except Exception:
                live = False
            last_seen = d.get("last_seen") or 0
            items.append({
                "device_id": did,
                "user_id": d.get("user_id") or "",
                "device_name": d.get("device_name") or "",
                "status": d.get("status") or "",
                "online": live,                       # 实时在线（心跳态）
                "online_stored": bool(d.get("online")),  # 落库在线（兜底）
                "last_seen": last_seen,
                "last_seen_rel": _rel(last_seen),
                "activated_at": d.get("activated_at") or 0,
            })
        # 排序：在线优先，其次按最近活跃倒序
        items.sort(key=lambda x: (not x["online"], -(x["last_seen"] or 0)))
        return _cors(web.json_response({
            "ok": True,
            "count": len(items),
            "data": items,
            "server_time": int(time.time()),
        }))
    except Exception as e:  # 绝不外溢
        return _cors(web.json_response(
            {"ok": False, "error": str(e)}, status=500))


async def handle_admin_options(request: web.Request):
    return _cors(web.Response(status=204))


@register_routes
def register(app):
    app.router.add_get("/api/v1/admin/devices", handle_admin_devices)
    app.router.add_options("/api/v1/admin/devices", handle_admin_options)
