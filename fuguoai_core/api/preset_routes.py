"""角色预设模板库 API（路由前缀 /api/v1/presets）。

本模块为**零侵入接入**：不修改既有 http_server 路由，仅通过
``fuguoai_core.api.registry`` 的 ``*_routes.py`` 命名约定被自动发现并挂载，
由 ``register`` 完成路由注册。

所有写操作统一经 ``api_guard`` 守卫：在尚未注入验证器前一律认定为未授权，
以 401 fail-closed 方式拒绝访问，杜绝"守卫缺失导致越权"。

三个接口：
- ``GET  /api/v1/presets``                     角色预设列表
- ``GET  /api/v1/presets/{preset_id}``         单个角色预设详情
- ``POST /api/v1/presets/{preset_id}/apply``   把预设套用到指定设备

错误码约定：
- 401 未通过守卫（fail-closed）
- 400 缺少 device_id
- 403 越权操作他人设备
- 404 预设不存在
- 422 校验失败 / 无可套用字段
- 500 落库失败
"""

import json
import logging
import sqlite3

from aiohttp import web

from fuguoai_core.api.registry import register_routes
from fuguoai_core.guards import api_guard
from fuguoai_core.utils.agent_config_store import get_agent_config_store
from fuguoai_core.utils.preset_store import get_all_presets, get_preset, preset_config
from fuguoai_core.device_access import DeviceBindingStore
from fuguoai_core.utils.util import normalize_device_id

TAG = "preset_routes"
logger = logging.getLogger(TAG)

_SPEED_MIN, _SPEED_MAX = 0.5, 2.0
_VOL_MIN, _VOL_MAX = 0.0, 1.0
_WAKEUP_MAX = 100
_PROMPT_MAX = 4000
_APPLY_FIELDS = (
    "wakeup_words",
    "prompt",
    "tts_provider",
    "voice",
    "speed",
    "volume",
    "tone",
    "dialect",
)


def _dumps(o):
    """JSON 序列化（保留中文等非 ASCII 字符原样输出）。"""
    return json.dumps(o, ensure_ascii=False)


def _add_cors(request, resp):
    """给响应补上统一的 CORS 响应头并返回该响应。"""
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "content-type, authorization"
    return resp


def _err(status, message):
    """统一错误响应体：{"code": <status>, "message": <message>}。"""
    return web.json_response({"code": status, "message": message}, status=status)


def _user_id(user: dict) -> str:
    """取用户标识：优先 user_id，回落 openid。"""
    user = user or {}
    return user.get("user_id") or user.get("openid")


def _owned_device(user: dict, device_id: str) -> bool:
    """判断 device_id 是否归属于该用户。"""
    uid = _user_id(user)
    binding = DeviceBindingStore()
    owned = None
    for d in binding.list_all():
        if d.get("device_id") == device_id:
            owned = d
            break
    return bool(owned and owned.get("user_id") == uid)


def _prepare_apply_fields(cfg: dict, preset_id: str = "") -> dict:
    """把预设配置裁剪/校验为可直接落库的字段 dict。

    按 _APPLY_FIELDS 固定顺序产出，键序即该顺序；不合法的字段跳过并记录告警。
    """
    fields = {}
    pfx = f"预设[{preset_id}] " if preset_id else "预设"
    for k in _APPLY_FIELDS:
        if k not in cfg:
            continue
        v = cfg[k]
        if v is None:
            # 显式 None 视为"不覆盖"，如未指定音色时不强制改动设备侧配置
            continue
        if k == "wakeup_words":
            if not isinstance(v, str) or len(v) > _WAKEUP_MAX:
                logger.warning(f"{pfx}字段 {k} 越界，已跳过")
                continue
        elif k == "prompt":
            if not isinstance(v, str) or len(v) > _PROMPT_MAX:
                logger.warning(f"{pfx}字段 {k} 越界，已跳过")
                continue
        elif k == "speed":
            try:
                v = float(v)
            except Exception:
                continue
            if not (_SPEED_MIN <= v <= _SPEED_MAX):
                logger.warning(f"{pfx}speed {v} 越界，已跳过")
                continue
        elif k == "volume":
            try:
                v = float(v)
            except Exception:
                continue
            if not (_VOL_MIN <= v <= _VOL_MAX):
                logger.warning(f"{pfx}volume {v} 越界，已跳过")
                continue
        elif k in ("tts_provider", "voice", "tone", "dialect"):
            if not isinstance(v, str) or not v:
                continue
        fields[k] = v
    return fields


@register_routes
def register(app):
    """注册角色预设相关路由。"""
    app.router.add_get("/api/v1/presets", handle_list)
    app.router.add_get("/api/v1/presets/{preset_id}", handle_get)
    app.router.add_post("/api/v1/presets/{preset_id}/apply", handle_apply)
    app.router.add_options("/api/v1/presets", handle_options)
    app.router.add_options("/api/v1/presets/{preset_id}/apply", handle_options)


async def handle_options(request):
    """预检请求：空体 200 + CORS。"""
    return _add_cors(request, web.Response(text=""))


async def handle_list(request):
    """角色预设列表。"""
    user = api_guard(request)
    if isinstance(user, web.Response):
        return _add_cors(request, user)
    return _add_cors(request, web.json_response({"ok": True, "presets": get_all_presets()}, dumps=_dumps))


async def handle_get(request):
    """单个角色预设详情。"""
    user = api_guard(request)
    if isinstance(user, web.Response):
        return _add_cors(request, user)
    preset_id = request.match_info.get("preset_id")
    preset = get_preset(preset_id)
    if not preset:
        return _add_cors(request, _err(404, f"预设不存在: {preset_id}"))
    return _add_cors(request, web.json_response({"ok": True, "preset": preset}, dumps=_dumps))


async def handle_apply(request):
    """把角色预设套用到指定设备的 agent-config。"""
    user = api_guard(request)
    if isinstance(user, web.Response):
        return _add_cors(request, user)

    preset_id = request.match_info.get("preset_id")
    if not get_preset(preset_id):
        return _add_cors(request, _err(404, f"预设不存在: {preset_id}"))

    device_id = request.query.get("device_id")
    if not device_id:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if isinstance(body, dict):
            device_id = body.get("device_id")

    device_id = normalize_device_id(device_id)
    if not device_id:
        return _add_cors(request, _err(400, "缺少 device_id（query 参数或 JSON body）"))

    if not _owned_device(user, device_id):
        return _add_cors(request, _err(403, "无权限操作该设备"))

    cfg = preset_config(preset_id) or {}
    fields = _prepare_apply_fields(cfg, preset_id=preset_id)
    if not fields:
        return _add_cors(request, _err(422, "预设无可套用字段"))

    fields["active_preset"] = preset_id

    store = get_agent_config_store()
    try:
        row = store.put_config(device_id, fields, user_id=_user_id(user))
    except sqlite3.Error as e:
        logger.error(f"套用预设写入 agent-config 失败 device={device_id} preset={preset_id}: {e}")
        return _add_cors(request, _err(500, "套用预设失败：设备配置写入异常"))

    return _add_cors(request, web.json_response(
        {
            "ok": True,
            "device_id": device_id,
            "preset_id": preset_id,
            "applied": fields,
            "updated_at": row.get("updated_at"),
        },
        dumps=_dumps,
    ))
