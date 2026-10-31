"""M12 设备智能体配置读写 API。

路由前缀 ``/api/v1/agent-config``，由 ``fuguoai_core.api.registry`` 按
``*_routes.py`` 命名约定**自动发现并挂载**，无需改动 ``http_server`` 既有路由，
对本项目的 HTTP 层零侵入。

所有接口统一经 ``core.guards.api_guard`` 守卫：守卫返回 ``web.Response``（401）时
原样透传，其余情况继续业务处理；每个响应都会补上 CORS 头。

接口清单：
    1. ``GET    /api/v1/agent-config``              列出当前用户可见设备的生效配置
    2. ``GET    /api/v1/agent-config/{device_id}``   读取单台设备的生效配置
    3. ``PUT    /api/v1/agent-config/{device_id}``   写入单台设备的配置覆盖（PATCH 语义）
    4. ``DELETE /api/v1/agent-config/{device_id}``   删除覆盖，回落到全局默认
    5. ``OPTIONS`` 预检（集合与单体两个路径共用同一处理器）

字段说明（PUT 仅校验请求体中出现的字段，未出现的字段不动）：
    ``wakeup_words`` / ``prompt`` / ``tts_provider`` / ``voice`` / ``speed`` /
    ``volume`` / ``tone`` / ``dialect`` / ``device_name``。
    ``speed`` 取值 0.5–2.0，``volume`` 取值 0.0–1.0；``device_name`` 会同时
    落库到设备绑定表。手动编辑即视为偏离预设，``active_preset`` 会被置空。

错误统一返回 ``{"code": int, "message": str}``，校验失败为 422。
"""

import json
import logging
import os
import time

import yaml
from aiohttp import web

from fuguoai_core.api.registry import register_routes
from fuguoai_core.device_access import DeviceBindingStore
from fuguoai_core.guards import api_guard
from fuguoai_core.utils.agent_config_store import get_agent_config_store
from fuguoai_core.utils.util import normalize_device_id

TAG = "agent_config_routes"
logger = logging.getLogger(TAG)

_SPEED_MIN, _SPEED_MAX = 0.5, 2.0
_VOL_MIN, _VOL_MAX = 0.0, 1.0
_WAKEUP_MAX = 100
_PROMPT_MAX = 4000

_GLOBAL_CFG_CACHE = {"cfg": None, "ts": 0.0}


def _global_config() -> dict:
    """读取全局 config.yaml，带 5 秒 TTL 缓存；失败返回 {} 且不污染缓存。"""
    now = time.time()
    cached = _GLOBAL_CFG_CACHE.get("cfg")
    if cached is not None and now - _GLOBAL_CFG_CACHE.get("ts", 0.0) < 5:
        return cached
    try:
        from fuguoai_config.config_loader import get_project_dir

        with open(os.path.join(get_project_dir(), "config.yaml"), "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        _GLOBAL_CFG_CACHE["cfg"] = cfg
        _GLOBAL_CFG_CACHE["ts"] = now
        return cfg
    except Exception as e:
        logger.warning(f"读取全局配置失败: {e}")
        return {}


def _dumps(o):
    return json.dumps(o, ensure_ascii=False)


def _add_cors(request, resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, PUT, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "content-type, authorization"
    return resp


def _err(status, message):
    return web.json_response({"code": status, "message": message}, status=status)


def _validate(payload: dict):
    """PATCH 语义：只校验 payload 中出现的字段。返回 (ok, message, cleaned)。"""
    cleaned = {}

    if "wakeup_words" in payload:
        v = payload["wakeup_words"]
        if not isinstance(v, str) or len(v) > _WAKEUP_MAX:
            return False, f"wakeup_words 应为不超过 {_WAKEUP_MAX} 字的字符串", {}
        cleaned["wakeup_words"] = v

    if "prompt" in payload:
        v = payload["prompt"]
        if not isinstance(v, str) or len(v) > _PROMPT_MAX:
            return False, f"prompt 应为不超过 {_PROMPT_MAX} 字的字符串", {}
        cleaned["prompt"] = v

    if "tts_provider" in payload:
        v = payload["tts_provider"]
        if not isinstance(v, str) or not v:
            return False, "tts_provider 不能为空字符串", {}
        supported = list((_global_config().get("TTS") or {}).keys())
        if supported and v not in supported:
            return False, f"tts_provider 不在已配置服务商列表: {supported}", {}
        cleaned["tts_provider"] = v

    if "voice" in payload:
        v = payload["voice"]
        if not isinstance(v, str) or not v:
            return False, "voice 不能为空字符串", {}
        cleaned["voice"] = v

    if "speed" in payload:
        try:
            v = float(payload["speed"])
        except (TypeError, ValueError):
            return False, "speed 应为数值", {}
        if not (_SPEED_MIN <= v <= _SPEED_MAX):
            return False, f"speed 应在 {_SPEED_MIN}–{_SPEED_MAX} 之间", {}
        cleaned["speed"] = v

    if "volume" in payload:
        try:
            v = float(payload["volume"])
        except (TypeError, ValueError):
            return False, "volume 应为数值", {}
        if not (_VOL_MIN <= v <= _VOL_MAX):
            return False, f"volume 应在 {_VOL_MIN}–{_VOL_MAX} 之间", {}
        cleaned["volume"] = v

    if "tone" in payload:
        v = payload["tone"]
        cleaned["tone"] = v if isinstance(v, str) else str(v)

    if "dialect" in payload:
        v = payload["dialect"]
        cleaned["dialect"] = v if isinstance(v, str) else str(v)

    if "device_name" in payload:
        v = payload["device_name"]
        if not isinstance(v, str) or not v or len(v) > 50:
            return False, "device_name 应为 1–50 字的非空字符串", {}
        cleaned["device_name"] = v

    return True, "", cleaned


def _effective(device_id) -> dict:
    """设备覆盖值 + 全局默认，合并出该设备的生效配置。"""
    store = get_agent_config_store()
    row = store.get_config(device_id) or {}
    g = _global_config()
    provider = row.get("tts_provider") or (g.get("selected_module") or {}).get("TTS")
    gv = ((g.get("TTS") or {}).get(provider) or {}) if provider else {}
    speed = row.get("speed")
    volume = row.get("volume")
    return {
        "device_id": device_id,
        "wakeup_words": row.get("wakeup_words"),
        "prompt": row.get("prompt") or g.get("prompt"),
        "tts_provider": provider,
        "voice": row.get("voice") or gv.get("voice"),
        "speed": speed if speed is not None else 1.0,
        "volume": volume if volume is not None else 1.0,
        "tone": row.get("tone"),
        "dialect": row.get("dialect"),
        "active_preset": row.get("active_preset"),
        "updated_at": row.get("updated_at"),
        "has_override": row.get("updated_at") is not None,
    }


def _user_id(user) -> str:
    return (user or {}).get("user_id") or (user or {}).get("openid")


async def handle_options(request):
    return _add_cors(request, web.Response(text=""))


async def handle_list(request):
    user = api_guard(request)
    if isinstance(user, web.Response):
        return _add_cors(request, user)

    uid = _user_id(user)
    store = get_agent_config_store()
    binding = DeviceBindingStore()
    _name_map = {d["device_id"]: d.get("device_name") for d in binding.list_all()}
    rows = store.list_for_user(uid) if uid else store.list_all_configured()

    devices = []
    for r in rows:
        did = r.get("device_id") if isinstance(r, dict) else r
        item = _effective(did)
        item["status"] = binding.get_status(did)
        item["device_name"] = _name_map.get(did)
        devices.append(item)

    return _add_cors(request, web.json_response({"ok": True, "devices": devices}, dumps=_dumps))


async def handle_get(request):
    user = api_guard(request)
    if isinstance(user, web.Response):
        return _add_cors(request, user)

    device_id = normalize_device_id(request.match_info.get("device_id"))
    return _add_cors(request, web.json_response(_effective(device_id), dumps=_dumps))


async def handle_put(request):
    user = api_guard(request)
    if isinstance(user, web.Response):
        return _add_cors(request, user)

    device_id = normalize_device_id(request.match_info.get("device_id"))

    binding = DeviceBindingStore()
    _owned = next((d for d in binding.list_all() if d.get("device_id") == device_id), None)
    if not _owned or _owned.get("user_id") != _user_id(user):
        return _add_cors(request, _err(403, "无权限操作该设备"))

    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return _add_cors(request, _err(400, "请求体应为 JSON 对象"))

    ok, msg, cleaned = _validate(payload)
    if not ok:
        return _add_cors(request, _err(422, msg))

    # M15：手动编辑即偏离预设
    cleaned["active_preset"] = None

    row = get_agent_config_store().put_config(device_id, cleaned, user_id=_user_id(user))

    # M16：设备名同步回绑定表
    if "device_name" in cleaned:
        DeviceBindingStore().set_device_name(device_id, cleaned["device_name"])

    return _add_cors(
        request,
        web.json_response(
            {"device_id": device_id, "updated_at": row["updated_at"], "applied": True},
            dumps=_dumps,
        ),
    )


async def handle_delete(request):
    user = api_guard(request)
    if isinstance(user, web.Response):
        return _add_cors(request, user)

    device_id = normalize_device_id(request.match_info.get("device_id"))

    binding = DeviceBindingStore()
    _owned = next((d for d in binding.list_all() if d.get("device_id") == device_id), None)
    if not _owned or _owned.get("user_id") != _user_id(user):
        return _add_cors(request, _err(403, "无权限操作该设备"))

    get_agent_config_store().delete_config(device_id)
    return _add_cors(
        request,
        web.json_response({"device_id": device_id, "restored": "global_default"}, dumps=_dumps),
    )


@register_routes
def register(app):
    app.router.add_get("/api/v1/agent-config", handle_list)
    app.router.add_get("/api/v1/agent-config/{device_id}", handle_get)
    app.router.add_put("/api/v1/agent-config/{device_id}", handle_put)
    app.router.add_delete("/api/v1/agent-config/{device_id}", handle_delete)
    app.router.add_options("/api/v1/agent-config", handle_options)
    app.router.add_options("/api/v1/agent-config/{device_id}", handle_options)
