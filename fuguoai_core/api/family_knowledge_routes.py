"""家庭知识录入管理 API（/api/v1/family-knowledge）。

零侵入接入：由 fuguoai_core.api.registry 自动发现并挂载，不改动 http_server 既有路由，
符合《长期维护更新计划》feature=family_knowledge 约定。让小程序主动录入/管理"家里的事"，
与维度⑤对话自动抽取（family_rag.extract_async）互补：自动沉淀 + 主动录入 双通道。

接口（前缀 /api/v1；[用户态守卫] 复用 core.guards.api_guard 小程序 JWT）
- GET    /api/v1/family-knowledge/{device_id}           列出该设备全部家庭知识（不含向量）
- POST   /api/v1/family-knowledge/{device_id}           新增/更新一条（body: content 必填, category?, tags?）
- DELETE /api/v1/family-knowledge/{device_id}           删除一条（query: fact_id）
- DELETE /api/v1/family-knowledge/{device_id}/all      清空全部（body: {"confirm":"YES"}）
- GET    /api/v1/family-knowledge/{device_id}/schema    类别元数据（前端渲染录入表单用）

设计要点
--------
- 设备归属：复用 DeviceBindingStore + normalize_device_id，与 persona_routes / devices_routes 同一套守卫；
  非本人/不存在统一 404，不泄露设备存在性。
- 存储 key：normalize_device_id 后的规范形态，与 connection.py:291 self.device_id 一致。
- 数据层：直接复用维度⑤ FamilyRAG（list_facts/upsert_fact/remove_fact/clear），
  文件层 data/family_rag/{device_id}.json；下一轮对话相关召回即生效，无需重启。
- POST 相同 content 视为同一条（sha1 同 id），更新其类别/标签，不新增副本。
- enabled 未开启时 GET 返回空列表、POST/DELETE 返回 503。
"""

import json
import logging

from aiohttp import web

from fuguoai_core.api.registry import register_routes
from fuguoai_core.guards import api_guard
from fuguoai_core.device_access import DeviceBindingStore
from fuguoai_core.utils.util import normalize_device_id
from fuguoai_core.utils.family_rag import get_family_rag
from fuguoai_core.utils.metrics import METRICS


TAG = "family_knowledge_routes"
logger = logging.getLogger(TAG)


# 类别元信息（供小程序渲染家庭知识录入表单）
_CATEGORY_META = {
    "family": {"label": "家庭成员", "hint": "如「爷爷、女儿小美」"},
    "pet": {"label": "宠物", "hint": "如「猫叫咪咪，橘猫」"},
    "habit": {"label": "生活习惯", "hint": "如「每天晚饭后散步」"},
    "schedule": {"label": "日程", "hint": "如「周三有钢琴课」"},
    "preference": {"label": "偏好", "hint": "如「不爱吃香菜」"},
    "item": {"label": "物品位置", "hint": "如「药箱在客厅抽屉」"},
    "health": {"label": "健康", "hint": "如「爸爸对花粉过敏」"},
    "other": {"label": "其他", "hint": "其他家庭事实"},
}


def _dumps(o):
    return json.dumps(o, ensure_ascii=False)


def _add_cors(request, resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "content-type, authorization"
    return resp


def _err(status, message):
    return web.json_response(
        {"code": status, "message": message}, status=status, dumps=_dumps
    )


def _ok(data):
    return web.json_response({"ok": True, "data": data}, dumps=_dumps)


def _resolve(request, raw_device_id):
    """鉴权 + 归属校验。返回 (err_response, canonical_did)；非本人/不存在统一 404。"""
    user = api_guard(request)
    if isinstance(user, web.Response):
        return user, None
    uid = (user or {}).get("user_id") or (user or {}).get("openid")
    did = normalize_device_id(raw_device_id)
    try:
        binding = DeviceBindingStore()
        for d in binding.list_all():
            if normalize_device_id(d.get("device_id", "")) == did:
                if d.get("user_id") == uid:
                    return None, did
                return _err(404, "设备不存在或无权访问"), None
    except Exception as e:
        logger.error(f"家庭知识归属查询失败: {e}")
        return _err(500, "归属查询失败"), None
    return _err(404, "设备不存在或无权访问"), None


def _store_or_err():
    store = get_family_rag()
    if store is None:
        return None, _err(503, "family_rag 模块未初始化")
    return store, None


def _inc_api(method, did):
    if METRICS:
        try:
            METRICS.inc(
                "fuguoai_family_knowledge_api_total",
                labels={"method": method, "device_id": did or "unknown"},
            )
        except Exception:
            pass


async def handle_list(request):
    """GET /api/v1/family-knowledge/{device_id} —— 列出全部家庭知识（不含向量）。"""
    err, did = _resolve(request, request.match_info["device_id"])
    if err:
        return err
    store, e = _store_or_err()
    if e:
        return e
    _inc_api("GET", did)
    try:
        facts = store.list_facts(did)
    except Exception as ex:
        logger.error(f"家庭知识读取失败 device={did}: {ex}")
        return _err(500, "读取失败")
    return _add_cors(request, _ok({
        "device_id": did,
        "enabled": store.enabled,
        "facts": facts,
    }))


async def handle_post(request):
    """POST /api/v1/family-knowledge/{device_id} —— 新增/更新一条。"""
    err, did = _resolve(request, request.match_info["device_id"])
    if err:
        return err
    store, e = _store_or_err()
    if e:
        return e
    if not store.enabled:
        return _err(503, "FamilyRAG 未启用")
    try:
        body = await request.json()
    except Exception:
        return _err(400, "请求体须为 JSON")
    if not isinstance(body, dict):
        return _err(400, "请求体须为对象")
    content = body.get("content")
    if not isinstance(content, str) or len(content.strip()) < 2:
        return _err(400, "content 须为至少 2 个字的字符串")
    category = body.get("category")
    if category is not None and not isinstance(category, str):
        return _err(400, "category 须为字符串")
    tags = body.get("tags")
    if tags is not None and not isinstance(tags, (list, str)):
        return _err(400, "tags 须为数组或逗号分隔字符串")
    try:
        fact = store.upsert_fact(did, content.strip(), category or "", tags)
    except Exception as ex:
        logger.error(f"家庭知识录入失败 device={did}: {ex}")
        return _err(500, "录入失败")
    if fact is None:
        return _err(500, "录入失败（embedding 异常）")
    _inc_api("POST", did)
    return _add_cors(request, _ok({"device_id": did, "fact": fact}))


async def handle_delete(request):
    """DELETE /api/v1/family-knowledge/{device_id}?fact_id=xxx —— 删除一条。"""
    err, did = _resolve(request, request.match_info["device_id"])
    if err:
        return err
    store, e = _store_or_err()
    if e:
        return e
    if not store.enabled:
        return _err(503, "FamilyRAG 未启用")
    fact_id = request.query.get("fact_id", "").strip()
    if not fact_id:
        return _err(400, "query 参数 fact_id 必填")
    try:
        removed = store.remove_fact(did, fact_id)
    except Exception as ex:
        logger.error(f"家庭知识删除失败 device={did}: {ex}")
        return _err(500, "删除失败")
    if not removed:
        return _err(404, "该家庭知识不存在")
    _inc_api("DELETE", did)
    return _add_cors(request, _ok({"device_id": did, "removed": True}))


async def handle_clear(request):
    """DELETE /api/v1/family-knowledge/{device_id}/all —— 清空全部（需 confirm）。"""
    err, did = _resolve(request, request.match_info["device_id"])
    if err:
        return err
    store, e = _store_or_err()
    if e:
        return e
    if not store.enabled:
        return _err(503, "FamilyRAG 未启用")
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict) or body.get("confirm") != "YES":
        return _err(400, "清空需 body 携带 {\"confirm\":\"YES\"}")
    try:
        n = store.clear(did)
    except Exception as ex:
        logger.error(f"家庭知识清空失败 device={did}: {ex}")
        return _err(500, "清空失败")
    _inc_api("DELETE_ALL", did)
    return _add_cors(request, _ok({"device_id": did, "removed": n}))


async def handle_schema(request):
    """GET /api/v1/family-knowledge/{device_id}/schema —— 类别元数据（静态，需登录，无需归属）。"""
    user = api_guard(request)
    if isinstance(user, web.Response):
        return user
    return _add_cors(request, _ok({
        "categories": _CATEGORY_META,
    }))


async def handle_options(request):
    return _add_cors(request, web.Response(status=204))


@register_routes
def register(app):
    app.router.add_get("/api/v1/family-knowledge/{device_id}", handle_list)
    app.router.add_post("/api/v1/family-knowledge/{device_id}", handle_post)
    app.router.add_delete("/api/v1/family-knowledge/{device_id}", handle_delete)
    app.router.add_delete("/api/v1/family-knowledge/{device_id}/all", handle_clear)
    app.router.add_get("/api/v1/family-knowledge/{device_id}/schema", handle_schema)
    app.router.add_options("/api/v1/family-knowledge/{device_id}", handle_options)
    app.router.add_options("/api/v1/family-knowledge/{device_id}/all", handle_options)
    app.router.add_options("/api/v1/family-knowledge/{device_id}/schema", handle_options)
