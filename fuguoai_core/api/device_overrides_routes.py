"""设备级配置覆盖查看 API（/api/v1/device-overrides）。

只读诊断接口：暴露设备对全局配置（尤其是 voice / tts_provider / 音色相关）在
data/device_agent.db 的覆盖值，便于排查「全局配置（data/.config.yaml）改了但不生效」——
多半是设备在 device_agent_config 表有覆盖。

零侵入接入：由 fuguoai_core.api.registry 自动发现并挂载，复用 core.guards.api_guard 小程序 JWT。
- GET /api/v1/device-overrides              返回当前用户名下所有设备的覆盖值
- GET /api/v1/device-overrides/{device_id}  返回单台设备的覆盖值（设备归属校验，同 persona 语义）
"""
import json
import logging

from aiohttp import web

from fuguoai_core.api.registry import register_routes
from fuguoai_core.guards import api_guard
from fuguoai_core.device_access import DeviceBindingStore
from fuguoai_core.utils.util import normalize_device_id
from fuguoai_core.utils.agent_config_store import get_agent_config_store

TAG = "device_overrides_routes"
logger = logging.getLogger(TAG)

# 暴露给运维/排查的覆盖维度（其余字段属角色画像，不在本接口范围）
_OVERLAY_FIELDS = ("tts_provider", "voice", "speed", "volume", "tone", "dialect", "active_preset")


def _dumps(o):
    return json.dumps(o, ensure_ascii=False)


def _err(status, message):
    return web.json_response(
        {"code": status, "message": message}, status=status, dumps=_dumps
    )


def _ok(data):
    return web.json_response({"ok": True, "data": data}, dumps=_dumps)


def _user_id(user):
    return (user or {}).get("user_id") or (user or {}).get("openid")


def _resolve(request, raw_device_id):
    """鉴权 + 归属校验，返回 (err_response, canonical_did)。"""
    user = api_guard(request)
    if isinstance(user, web.Response):
        return user, None
    uid = _user_id(user)
    did = normalize_device_id(raw_device_id)
    try:
        binding = DeviceBindingStore()
        for d in binding.list_all():
            if normalize_device_id(d.get("device_id", "")) == did:
                if d.get("user_id") == uid:
                    return None, did
                return _err(404, "设备不存在或无权访问"), None
    except Exception as e:
        logger.error(f"device-overrides 归属查询失败: {e}")
        return _err(500, "归属查询失败"), None
    return _err(404, "设备不存在或无权访问"), None


def _overlay_only(cfg):
    if not cfg:
        return None
    return {k: cfg.get(k) for k in _OVERLAY_FIELDS}


async def handle_list(request):
    """GET /api/v1/device-overrides —— 当前用户名下所有设备的覆盖值。"""
    user = api_guard(request)
    if isinstance(user, web.Response):
        return user
    uid = _user_id(user)
    try:
        store = get_agent_config_store()
        rows = store.list_for_user(uid) if store else []
    except Exception as e:
        logger.error(f"device-overrides 列表失败: {e}")
        return _err(500, "查询失败")
    return _ok([
        {"device_id": r.get("device_id"), "overlay": _overlay_only(r)}
        for r in rows
    ])


async def handle_get(request):
    """GET /api/v1/device-overrides/{device_id} —— 单设备覆盖值。"""
    err, did = _resolve(request, request.match_info["device_id"])
    if err:
        return err
    try:
        store = get_agent_config_store()
        cfg = store.get_config(did) if store else None
    except Exception as e:
        logger.error(f"device-overrides 读取失败 device={did}: {e}")
        return _err(500, "读取失败")
    return _ok({"device_id": did, "overlay": _overlay_only(cfg)})


@register_routes
def register(app):
    app.router.add_get("/api/v1/device-overrides", handle_list)
    app.router.add_get("/api/v1/device-overrides/{device_id}", handle_get)
