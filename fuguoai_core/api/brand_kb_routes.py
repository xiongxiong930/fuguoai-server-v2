"""复果品牌知识库 · 内容审核 API（/api/v1/admin/brand-kb）。

审核流（#144 示范）：KB 条目拥有 status 生命周期
``draft → published | rejected``，只有 ``published`` 才进检索 / core 注入链路；
``draft``（待审）/ ``rejected``（已拒）不对外生效，直到经本 API 置为 ``published``。

鉴权：复用 admin 令牌（``FUGUOAI_ADMIN_TOKEN`` / ``FUGUOAI_VOICE_ADMIN_TOKEN`` 回退），
与 admin_cards_routes.py 同范式；响应信封对齐 43 管理后台 ``{code, message, data}``。

自动挂载：文件名以 ``_routes.py`` 结尾，由 ``fuguoai_core.api.registry.apply_routes``
按命名约定自动发现并挂载，fail-soft：本模块异常不影响核心路由。

端点
----
- ``POST /api/v1/admin/brand-kb/docs``                  提交草稿（draft，不直接发布）
- ``GET  /api/v1/admin/brand-kb/docs?status=draft``     列表（可选按 status 过滤）
- ``GET  /api/v1/admin/brand-kb/docs/{doc_id}``         单条详情
- ``POST /api/v1/admin/brand-kb/docs/{doc_id}/publish`` 审核通过（→ published）
- ``POST /api/v1/admin/brand-kb/docs/{doc_id}/reject``  审核拒绝（→ rejected）
"""

import os

from aiohttp import web

from fuguoai_config.logger import setup_logging
from fuguoai_core.api.registry import register_routes
from fuguoai_core.utils.brand_kb import get_brand_kb, _DEFAULT_TENANT

logger = setup_logging()

_CATEGORIES = {"core", "longtail", "other"}


# ------------------------------------------------------------------
# 鉴权 / CORS / 信封（与 admin_cards_routes.py 同范式）
# ------------------------------------------------------------------
def _is_admin(request) -> bool:
    token = (os.environ.get("FUGUOAI_ADMIN_TOKEN")
             or os.environ.get("FUGUOAI_VOICE_ADMIN_TOKEN") or "").strip()
    if not token:
        return False
    auth = request.headers.get("Authorization", "")
    return auth.startswith("Bearer ") and auth[len("Bearer "):].strip() == token


def _cors(resp: web.Response) -> web.Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


def _ok(data=None, message="ok"):
    return _cors(web.json_response({"code": 0, "message": message, "data": data}))


def _err(code: int, message: str, http: int = 400):
    return _cors(web.json_response(
        {"code": code, "message": message, "data": None}, status=http))


async def _json_body(request) -> dict:
    """安全解析 JSON body；无 body 或解析失败一律回 {}，避免 415 拖垮接口。"""
    try:
        return await request.json() or {}
    except Exception:
        return {}


def _bkb():
    return get_brand_kb()


# ------------------------------------------------------------------
# 路由注册（自动发现挂载）
# ------------------------------------------------------------------
@register_routes
def register_brand_kb_routes(app):
    app.router.add_post("/api/v1/admin/brand-kb/docs", _handle_submit)
    app.router.add_get("/api/v1/admin/brand-kb/docs", _handle_list)
    app.router.add_get("/api/v1/admin/brand-kb/docs/{doc_id}", _handle_get)
    app.router.add_post("/api/v1/admin/brand-kb/docs/{doc_id}/publish", _handle_publish)
    app.router.add_post("/api/v1/admin/brand-kb/docs/{doc_id}/reject", _handle_reject)
    app.router.add_options("/api/v1/admin/brand-kb/{tail:.*}", _handle_options)


async def _handle_options(request):
    return _cors(web.Response())


# ------------------------------------------------------------------
# 处理器
# ------------------------------------------------------------------
async def _handle_submit(request: web.Request):
    """提交一条草稿（draft）。默认不直接发布，需经审核。"""
    if not _is_admin(request):
        return _err(403, "admin token required", 403)
    body = await _json_body(request)
    title = (body.get("title") or "").strip()
    text = (body.get("text") or "").strip()
    category = body.get("category") or "longtail"
    if category not in _CATEGORIES:
        category = "other"
    tags = body.get("tags")
    tenant_id = (body.get("tenant_id") or "").strip() or _DEFAULT_TENANT
    if not text or len(text) < 2:
        return _err(400, "text required (>=2 chars)")
    bkb = _bkb()
    if not bkb.enabled:
        return _err(503, "brand kb disabled")
    try:
        doc = bkb.add_doc(title, text, category=category, tags=tags, status="draft", tenant_id=tenant_id)
    except Exception as e:
        return _err(500, f"submit failed: {e}", 500)
    if doc is None:
        return _err(500, "submit failed (embedding error?)", 500)
    return _ok({"id": doc["id"], "status": doc["status"], "title": title, "tenant_id": tenant_id}, "submitted as draft")


async def _handle_list(request: web.Request):
    """GET /api/v1/admin/brand-kb/docs?status=draft&tenant_id=xxx —— 列表（按 status / tenant 过滤）。

    tenant_id 缺省为跨租户全量（管理视图）；传入则按租户过滤。
    """
    if not _is_admin(request):
        return _err(403, "admin token required", 403)
    status = (request.query.get("status") or "").strip() or None
    tenant_id = (request.query.get("tenant_id") or "").strip() or None
    bkb = _bkb()
    try:
        docs = bkb.list_docs(status_filter=status, tenant_id=tenant_id)
    except Exception as e:
        return _err(500, f"list failed: {e}", 500)
    return _ok({"items": docs, "total": len(docs)})


async def _handle_get(request: web.Request):
    """GET /api/v1/admin/brand-kb/docs/{doc_id}?tenant_id=xxx —— 单条详情（tenant_id 缺省跨租户）。"""
    if not _is_admin(request):
        return _err(403, "admin token required", 403)
    doc_id = request.match_info.get("doc_id", "")
    tenant_id = (request.query.get("tenant_id") or "").strip() or None
    doc = _bkb().get_doc(doc_id, tenant_id=tenant_id)
    if doc is None:
        return _err(404, "doc not found", 404)
    return _ok(doc)


async def _handle_publish(request: web.Request):
    """POST /api/v1/admin/brand-kb/docs/{doc_id}/publish —— 审核通过（→ published）。"""
    if not _is_admin(request):
        return _err(403, "admin token required", 403)
    doc_id = request.match_info.get("doc_id", "")
    body = await _json_body(request)
    reviewer = (body.get("reviewer") or "").strip() or "admin"
    tenant_id = (body.get("tenant_id") or "").strip() or None
    try:
        doc = _bkb().set_status(doc_id, "published", reviewer=reviewer, note=body.get("note"), tenant_id=tenant_id)
    except ValueError as e:
        return _err(400, str(e), 400)
    if doc is None:
        return _err(404, "doc not found", 404)
    return _ok({"id": doc["id"], "status": doc["status"]}, "published")


async def _handle_reject(request: web.Request):
    """POST /api/v1/admin/brand-kb/docs/{doc_id}/reject —— 审核拒绝（→ rejected）。"""
    if not _is_admin(request):
        return _err(403, "admin token required", 403)
    doc_id = request.match_info.get("doc_id", "")
    body = await _json_body(request)
    reviewer = (body.get("reviewer") or "").strip() or "admin"
    tenant_id = (body.get("tenant_id") or "").strip() or None
    try:
        doc = _bkb().set_status(doc_id, "rejected", reviewer=reviewer, note=body.get("note"), tenant_id=tenant_id)
    except ValueError as e:
        return _err(400, str(e), 400)
    if doc is None:
        return _err(404, "doc not found", 404)
    return _ok({"id": doc["id"], "status": doc["status"]}, "rejected")
