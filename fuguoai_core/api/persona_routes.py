"""设备人格管理 API（/api/v1/persona）。

零侵入接入：由 fuguoai_core.api.registry 自动发现并挂载，不改动 http_server 既有路由，
符合《长期维护更新计划》feature=persona 约定。

接口（前缀 /api/v1，feature=persona；[用户态守卫] 复用 core.guards.api_guard 小程序 JWT）
- GET    /api/v1/persona/{device_id}          读取该设备合并画像（含默认/配置层），便于小程序展示当前生效值
- PUT    /api/v1/persona/{device_id}          部分更新文件层（仅传要改字段；显式 null 清除；列表字段支持逗号串）
- DELETE /api/v1/persona/{device_id}          清除文件层，回到默认/配置层
- GET    /api/v1/persona/{device_id}/schema   返回字段定义（前端渲染表单用，静态、无需归属）

设计要点
--------
- 设备归属：复用 DeviceBindingStore + normalize_device_id，与 devices_routes 同一套守卫语义；
  非本人/不存在统一 403/404，不泄露设备存在性。
- 存储 key：必须用 normalize_device_id 后的规范形态，与 connection.py:291 的 self.device_id
  完全一致——否则 API 设的人格设备侧 render 读不到。
- 数据层：直接复用维度②E2 已落地的 PersonaStore（get_persona/update_persona/set_persona），
  文件层写入 data/.persona.yaml，下一轮对话 mtime 失效即生效，无需重启。
- 进程单例：get_persona_store() 在 connection._initialize_components 已初始化，handler 直接取用。
"""

import json
import logging

from aiohttp import web

from fuguoai_core.api.registry import register_routes
from fuguoai_core.guards import api_guard
from fuguoai_core.device_access import DeviceBindingStore
from fuguoai_core.utils.util import normalize_device_id
from fuguoai_core.utils.persona import get_persona_store, _FIELDS
from fuguoai_core.utils.metrics import METRICS


TAG = "persona_routes"
logger = logging.getLogger(TAG)


# 字段元信息（供小程序渲染人格编辑表单）
_FIELDS_META = {
    "name": {"label": "自称", "type": "string", "hint": "AI 用来称呼自己的名字，如「小福」", "max_len": 16},
    "style": {"label": "语气风格", "type": "string", "hint": "如「温暖、简洁、自然口语」", "max_len": 64},
    "traits": {"label": "性格特点", "type": "list", "hint": "多个用逗号分隔，如「幽默,有耐心」"},
    "catchphrase": {"label": "口头禅", "type": "string", "hint": "偶尔会蹦出的口头禅，如「没问题包在我身上」", "max_len": 32},
    "taboo": {"label": "禁忌话题", "type": "list", "hint": "绝不主动提及的话题，多个用逗号分隔"},
}


def _dumps(o):
    return json.dumps(o, ensure_ascii=False)


def _add_cors(request, resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, PUT, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "content-type, authorization"
    return resp


def _err(status, message):
    return web.json_response(
        {"code": status, "message": message}, status=status, dumps=_dumps
    )


def _ok(data):
    return web.json_response({"ok": True, "data": data}, dumps=_dumps)


def _user_id(user):
    return (user or {}).get("user_id") or (user or {}).get("openid")


def _resolve(request, raw_device_id):
    """鉴权 + 归属校验。

    返回 (err_response, canonical_did)：
    - err_response 非空 => 鉴权/归属失败，直接 return 它；
    - canonical_did 为 normalize 后的规范 id（与 connection.py 的 self.device_id 一致）。
    非本人与不存在统一返回 404，不泄露设备存在性。
    """
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
        logger.error(f"persona 归属查询失败: {e}")
        return _err(500, "归属查询失败"), None
    return _err(404, "设备不存在或无权访问"), None


def _store_or_err():
    store = get_persona_store()
    if store is None:
        return None, _err(503, "persona 模块未初始化")
    return store, None


def _inc_api(method, did):
    if METRICS:
        try:
            METRICS.inc("fuguoai_persona_api_total",
                       labels={"method": method, "device_id": did or "unknown"},
                       help_text="人格管理 API 调用次数(按方法/设备)")
        except Exception:
            pass


async def handle_get(request):
    """GET /api/v1/persona/{device_id} —— 读合并画像 + 文件层自定义部分。"""
    err, did = _resolve(request, request.match_info["device_id"])
    if err:
        return err
    store, e = _store_or_err()
    if e:
        return e
    _inc_api("GET", did)
    try:
        merged = store.get_persona(did)
        custom = store.get_file_layer(did)
    except Exception as ex:
        logger.error(f"persona 读取失败 device={did}: {ex}")
        return _err(500, "读取失败")
    return _add_cors(request, _ok({
        "device_id": did,
        "enabled": store.enabled,
        "persona": merged,
        "custom": custom,
    }))


async def handle_put(request):
    """PUT /api/v1/persona/{device_id} —— 部分更新文件层。"""
    err, did = _resolve(request, request.match_info["device_id"])
    if err:
        return err
    store, e = _store_or_err()
    if e:
        return e
    try:
        body = await request.json()
    except Exception:
        return _err(400, "请求体须为 JSON")
    if not isinstance(body, dict):
        return _err(400, "请求体须为对象")
    # 只允许已知字段；剔除其余，避免误写
    patch = {k: body[k] for k in _FIELDS if k in body}
    if not patch:
        return _err(400, f"未提供任何可更新字段（允许：{', '.join(_FIELDS)}）")
    try:
        merged = store.update_persona(did, patch)
    except ValueError as ve:
        return _err(400, str(ve))
    except Exception as ex:
        logger.error(f"persona 更新失败 device={did}: {ex}")
        return _err(500, "更新失败")
    _inc_api("PUT", did)
    try:
        custom = store.get_file_layer(did)
    except Exception:
        custom = {}
    return _add_cors(request, _ok({
        "device_id": did,
        "persona": merged,
        "custom": custom,
    }))


async def handle_delete(request):
    """DELETE /api/v1/persona/{device_id} —— 清除文件层（回到默认/配置层）。"""
    err, did = _resolve(request, request.match_info["device_id"])
    if err:
        return err
    store, e = _store_or_err()
    if e:
        return e
    try:
        merged = store.set_persona(did, {})
    except Exception as ex:
        logger.error(f"persona 清除失败 device={did}: {ex}")
        return _err(500, "清除失败")
    _inc_api("DELETE", did)
    return _add_cors(request, _ok({
        "device_id": did,
        "persona": merged,
        "custom": {},
    }))


async def handle_schema(request):
    """GET /api/v1/persona/{device_id}/schema —— 字段元数据（静态，需登录，无需设备归属）。"""
    user = api_guard(request)
    if isinstance(user, web.Response):
        return user
    return _add_cors(request, _ok({
        "fields": _FIELDS_META,
        "merge_layers": ["config.persona.default", "config.persona.devices", "data/.persona.yaml(本API写入)"],
    }))


async def handle_options(request):
    return _add_cors(request, web.Response(status=204))


@register_routes
def register(app):
    app.router.add_get("/api/v1/persona/{device_id}", handle_get)
    app.router.add_put("/api/v1/persona/{device_id}", handle_put)
    app.router.add_delete("/api/v1/persona/{device_id}", handle_delete)
    app.router.add_get("/api/v1/persona/{device_id}/schema", handle_schema)
    app.router.add_options("/api/v1/persona/{device_id}", handle_options)
    app.router.add_options("/api/v1/persona/{device_id}/schema", handle_options)
