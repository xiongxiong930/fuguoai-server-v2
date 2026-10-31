"""天使心 App 内容层 REST（新增路由，HTTP 只扩不删改）。

补齐《天使心后端接口文档 v0.1》中**此前只有文档、没有代码**的四节：

- §7 记忆痕迹：GET/POST  /api/v1/characters/{character_id}/memories
             GET         /api/v1/memories?character_id=   （扁平路径，契约对齐）
             DELETE      /api/v1/memories/{memory_id}     （单条删，P0）
             DELETE      /api/v1/memories?character_id=    （按心清空，P0）
- §8 首页心核：GET /api/v1/home/core、GET /api/v1/home/memories、PUT /api/v1/home/diy
- §9 手帐本： GET/POST /api/v1/journals、GET/PUT/DELETE /api/v1/journals/{id}、
             POST /api/v1/journals/{id}/share
- §10 个人中心：GET/PUT /api/v1/users/me、GET/PUT /api/v1/users/preferences
  - 心的资料覆盖约定（前端契约）：用 prefs 的 `character_overrides` 键，
    value = `{ "<character_id>": { "name":…, "personality":…, "voice":… } }`；
    写入**按 character_id 合并**（写 A 心绝不冲掉 B 心），可写字段仅
    name/personality/voice（gender/age/system_prompt 归 admin，客户端写不进来）。

记忆 / 性格闭环补充端点（用户拍板，2026-10-09）：
- GET  /api/v1/memories?character_id=            列表（与 /characters/{id}/memories 并存）
- DELETE /api/v1/memories/{id}                   单条删（越权 404，不泄露存在）
- DELETE /api/v1/memories?character_id=          按心清空
- GET/PUT /api/v1/characters/{id}/persona-offset 性格偏移层：**只读 + 一键重置**，
  PUT 不做任意写（基线 system_prompt 锁定）；维度有界，偏移由后端抽取管线增量更新。

**未实现**：§11 文件上传（multipart）。原因：它要定「文件存哪、怎么对外服务 URL」，
属于部署形态决策（容器存储 / 静态路由 / 容量），不该由代码层单方面拍板，故留给后续。
手帐 v1 的照片字段按 URL 字符串存（content.photos 为字符串数组），不依赖上传端点。

设计要点
--------
- 统一走 core.guards.api_guard（小程序 JWT），所有数据**按 uid 隔离**：越权一律 404
  或空列表，不泄露资源是否存在。
- 响应体与 character_routes 完全一致（`{code,message,data}`），前端一套解析即可。
- 心核的 primary_character 读角色会话（NFC 碰卡 / 小程序激活的那个），单一数据源。
- 存储层见 utils/angel_heart_store.py（data/angel_heart/*.json，锁 + 原子写）。
"""

import json
import logging

from aiohttp import web

from fuguoai_core.api.registry import register_routes
from fuguoai_core.guards import api_guard
from fuguoai_core.utils.angel_heart_store import (
    get_journal_store,
    get_memory_store,
    get_home_store,
    get_user_store,
    get_persona_store,
    _CATEGORY_ALIASES,
    _CATEGORIES,
    _VISIBILITIES,
)

TAG = "angel_heart_routes"
logger = logging.getLogger(TAG)


# ---------------------------------------------------------------------------
# 统一响应封装（与 character_routes / 文档 §2 一致：{code,message,data}）
# ---------------------------------------------------------------------------
def _dumps(o):
    return json.dumps(o, ensure_ascii=False)


def _add_cors(request, resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "content-type, authorization"
    return resp


def _ok(data):
    return web.json_response({"code": 0, "message": "", "data": data}, dumps=_dumps)


def _err(status, message):
    return web.json_response({"code": status, "message": message}, status=status, dumps=_dumps)


def _user_id(user):
    return (user or {}).get("user_id") or (user or {}).get("openid")


async def _auth(request):
    """返回 (uid, err_resp)。err_resp 非空则调用方直接 return。"""
    user = api_guard(request)
    if isinstance(user, web.Response):
        return None, _add_cors(request, user)
    uid = _user_id(user)
    if not uid:
        return None, _add_cors(request, _err(401, "未登录或用户标识缺失"))
    return uid, None


async def _body(request):
    try:
        return await request.json()
    except Exception:
        return {}


def _parse_draft(raw):
    raw = (raw or "").strip().lower()
    if raw in ("1", "true", "yes"):
        return True
    if raw in ("0", "false", "no"):
        return False
    return None


# ---------------------------------------------------------------------------
# §7 记忆痕迹
# ---------------------------------------------------------------------------
async def handle_list_memories(request):
    uid, err = await _auth(request)
    if err:
        return err
    cid = request.match_info["character_id"].strip()
    items = get_memory_store().list(uid, cid)
    return _add_cors(request, _ok({"character_id": cid, "count": len(items), "memories": items}))


async def handle_add_memory(request):
    uid, err = await _auth(request)
    if err:
        return err
    cid = request.match_info["character_id"].strip()
    body = await _body(request)
    fact = ((body or {}).get("fact") or "").strip()
    if not fact:
        return _add_cors(request, _err(400, "fact 必填"))
    item = get_memory_store().add(uid, cid, fact)
    return _add_cors(request, _ok(item))


# —— 记忆闭环补充端点（用户拍板 2026-10-09）——
async def handle_list_memories_flat(request):
    """GET /api/v1/memories?character_id= —— 扁平列表路径（与
    /characters/{id}/memories 并存，数据一致）。character_id 必填。"""
    uid, err = await _auth(request)
    if err:
        return err
    cid = (request.query.get("character_id") or "").strip()
    if not cid:
        return _add_cors(request, _err(400, "character_id 必填"))
    items = get_memory_store().list(uid, cid)
    return _add_cors(request, _ok({"character_id": cid, "count": len(items), "memories": items}))


async def handle_delete_memory(request):
    """DELETE /api/v1/memories/{memory_id} —— 单条删。
    越权 / 不存在统一 404，不泄露资源是否存在。"""
    uid, err = await _auth(request)
    if err:
        return err
    mid = request.match_info["memory_id"].strip()
    ok = get_memory_store().delete(uid, mid)
    if not ok:
        return _add_cors(request, _err(404, "记忆不存在"))
    return _add_cors(request, _ok({"deleted": True}))


async def handle_delete_memories_by_character(request):
    """DELETE /api/v1/memories?character_id= —— 按心清空。"""
    uid, err = await _auth(request)
    if err:
        return err
    cid = (request.query.get("character_id") or "").strip()
    if not cid:
        return _add_cors(request, _err(400, "character_id 必填"))
    n = get_memory_store().delete_all(uid, cid)
    return _add_cors(request, _ok({"character_id": cid, "deleted": n}))


# —— 性格偏移层：只读 + 重置（基线 system_prompt 锁定）——
async def handle_get_persona_offset(request):
    """GET /api/v1/characters/{id}/persona-offset —— 查看当前偏移与维度边界。"""
    uid, err = await _auth(request)
    if err:
        return err
    cid = request.match_info["character_id"].strip()
    return _add_cors(request, _ok(get_persona_store().get(uid, cid)))


async def handle_reset_persona_offset(request):
    """PUT /api/v1/characters/{id}/persona-offset —— 一键重置为初始（维度归零）。
    不做任意写：基线 system_prompt 永不经此改写，防漂移 / injection / 跨设备破坏。"""
    uid, err = await _auth(request)
    if err:
        return err
    cid = request.match_info["character_id"].strip()
    return _add_cors(request, _ok(get_persona_store().reset(uid, cid)))


# ---------------------------------------------------------------------------
# §8 首页 / 心核
# ---------------------------------------------------------------------------
async def handle_home_core(request):
    uid, err = await _auth(request)
    if err:
        return err
    return _add_cors(request, _ok(get_home_store().get_core(uid)))


async def handle_home_memories(request):
    uid, err = await _auth(request)
    if err:
        return err
    items = get_memory_store().list_all(uid)
    return _add_cors(request, _ok({"count": len(items), "memories": items}))


async def handle_home_diy(request):
    uid, err = await _auth(request)
    if err:
        return err
    body = await _body(request)
    if not isinstance(body, dict):
        return _add_cors(request, _err(400, "请求体须为对象"))
    patch = {k: body[k] for k in ("color", "form", "motion") if k in body}
    if not patch:
        return _add_cors(request, _err(400, "未提供任何可更新字段（允许：color, form, motion）"))
    return _add_cors(request, _ok(get_home_store().set_diy(uid, patch)))


# ---------------------------------------------------------------------------
# §9 手帐本
# ---------------------------------------------------------------------------
async def handle_list_journals(request):
    uid, err = await _auth(request)
    if err:
        return err
    raw_cat = (request.query.get("category") or "").strip()
    category = None
    if raw_cat:
        category = _CATEGORY_ALIASES.get(raw_cat)
        if category is None:
            return _add_cors(request, _err(400, f"category 无效（可选：{', '.join(sorted(_CATEGORY_ALIASES))}）"))
    raw_draft = request.query.get("draft")
    draft = _parse_draft(raw_draft)
    if raw_draft is not None and raw_draft != "" and draft is None:
        return _add_cors(request, _err(400, "draft 须为 true/false"))
    items = get_journal_store().list(uid, category=category, draft=draft)
    return _add_cors(request, _ok({"count": len(items), "journals": items}))


async def handle_create_journal(request):
    uid, err = await _auth(request)
    if err:
        return err
    body = await _body(request)
    if not isinstance(body, dict):
        return _add_cors(request, _err(400, "请求体须为对象"))
    raw_cat = body.get("category") or "daily"
    cat = _CATEGORY_ALIASES.get(raw_cat)
    if cat is None or cat == "draft":
        return _add_cors(request, _err(400, f"category 无效（可选：{', '.join(_CATEGORIES)}）"))
    body = dict(body)
    body["category"] = cat
    j = get_journal_store().create(uid, body)
    return _add_cors(request, _ok(j))


async def handle_get_journal(request):
    uid, err = await _auth(request)
    if err:
        return err
    j = get_journal_store().get(uid, request.match_info["journal_id"])
    if not j:
        return _add_cors(request, _err(404, "手帐不存在"))
    return _add_cors(request, _ok(j))


async def handle_update_journal(request):
    uid, err = await _auth(request)
    if err:
        return err
    body = await _body(request)
    if not isinstance(body, dict):
        return _add_cors(request, _err(400, "请求体须为对象"))
    patch = {k: body[k] for k in ("category", "draft", "title", "content") if k in body}
    if not patch:
        return _add_cors(request, _err(400, "未提供任何可更新字段（允许：category, draft, title, content）"))
    if "category" in patch:
        c = _CATEGORY_ALIASES.get(patch["category"])
        if c is None or c == "draft":
            return _add_cors(request, _err(400, f"category 无效（可选：{', '.join(_CATEGORIES)}）"))
        patch["category"] = c
    try:
        j = get_journal_store().update(uid, request.match_info["journal_id"], patch)
    except ValueError as ve:
        return _add_cors(request, _err(400, str(ve)))
    if not j:
        return _add_cors(request, _err(404, "手帐不存在"))
    return _add_cors(request, _ok(j))


async def handle_delete_journal(request):
    uid, err = await _auth(request)
    if err:
        return err
    ok = get_journal_store().delete(uid, request.match_info["journal_id"])
    if not ok:
        return _add_cors(request, _err(404, "手帐不存在"))
    return _add_cors(request, _ok({"deleted": True}))


async def handle_share_journal(request):
    uid, err = await _auth(request)
    if err:
        return err
    body = await _body(request)
    vis = ((body or {}).get("visibility") or "").strip()
    if vis not in _VISIBILITIES:
        return _add_cors(request, _err(400, f"visibility 须为 {_VISIBILITIES}"))
    j = get_journal_store().set_visibility(uid, request.match_info["journal_id"], vis)
    if not j:
        return _add_cors(request, _err(404, "手帐不存在"))
    return _add_cors(request, _ok({"id": j["id"], "visibility": j["visibility"]}))


# ---------------------------------------------------------------------------
# §10 个人中心
# ---------------------------------------------------------------------------
async def handle_get_me(request):
    uid, err = await _auth(request)
    if err:
        return err
    return _add_cors(request, _ok(get_user_store().get(uid)))


async def handle_put_me(request):
    uid, err = await _auth(request)
    if err:
        return err
    body = await _body(request)
    if not isinstance(body, dict):
        return _add_cors(request, _err(400, "请求体须为对象"))
    patch = {k: body[k] for k in ("nickname", "avatar") if k in body}
    if not patch:
        return _add_cors(request, _err(400, "未提供任何可更新字段（允许：nickname, avatar）"))
    return _add_cors(request, _ok(get_user_store().update(uid, patch)))


async def handle_get_preferences(request):
    uid, err = await _auth(request)
    if err:
        return err
    return _add_cors(request, _ok(get_user_store().get_preferences(uid)))


async def handle_put_preferences(request):
    uid, err = await _auth(request)
    if err:
        return err
    body = await _body(request)
    if not isinstance(body, dict):
        return _add_cors(request, _err(400, "请求体须为对象"))
    try:
        prefs = get_user_store().set_preferences(uid, body)
    except ValueError as ve:
        return _add_cors(request, _err(400, str(ve)))
    return _add_cors(request, _ok(prefs))


async def handle_options(request):
    return _add_cors(request, web.Response(status=204))


# ---------------------------------------------------------------------------
# 路由注册（registry 自动发现，无需改 http_server）
# ---------------------------------------------------------------------------
_PATHS = (
    "/api/v1/characters/{character_id}/memories",
    "/api/v1/memories",
    "/api/v1/memories/{memory_id}",
    "/api/v1/characters/{character_id}/persona-offset",
    "/api/v1/home/core",
    "/api/v1/home/memories",
    "/api/v1/home/diy",
    "/api/v1/journals",
    "/api/v1/journals/{journal_id}",
    "/api/v1/journals/{journal_id}/share",
    "/api/v1/users/me",
    "/api/v1/users/preferences",
)


@register_routes
def register(app):
    # §7 记忆痕迹
    app.router.add_get("/api/v1/characters/{character_id}/memories", handle_list_memories)
    app.router.add_post("/api/v1/characters/{character_id}/memories", handle_add_memory)
    # 记忆闭环补充端点（扁平路径 + 删除）
    app.router.add_get("/api/v1/memories", handle_list_memories_flat)
    app.router.add_delete("/api/v1/memories", handle_delete_memories_by_character)
    app.router.add_delete("/api/v1/memories/{memory_id}", handle_delete_memory)
    # 性格偏移层：只读 + 重置
    app.router.add_get("/api/v1/characters/{character_id}/persona-offset", handle_get_persona_offset)
    app.router.add_put("/api/v1/characters/{character_id}/persona-offset", handle_reset_persona_offset)
    # §8 首页 / 心核
    app.router.add_get("/api/v1/home/core", handle_home_core)
    app.router.add_get("/api/v1/home/memories", handle_home_memories)
    app.router.add_put("/api/v1/home/diy", handle_home_diy)
    # §9 手帐本
    app.router.add_get("/api/v1/journals", handle_list_journals)
    app.router.add_post("/api/v1/journals", handle_create_journal)
    app.router.add_get("/api/v1/journals/{journal_id}", handle_get_journal)
    app.router.add_put("/api/v1/journals/{journal_id}", handle_update_journal)
    app.router.add_delete("/api/v1/journals/{journal_id}", handle_delete_journal)
    app.router.add_post("/api/v1/journals/{journal_id}/share", handle_share_journal)
    # §10 个人中心
    app.router.add_get("/api/v1/users/me", handle_get_me)
    app.router.add_put("/api/v1/users/me", handle_put_me)
    app.router.add_get("/api/v1/users/preferences", handle_get_preferences)
    app.router.add_put("/api/v1/users/preferences", handle_put_preferences)
    for p in _PATHS:
        app.router.add_options(p, handle_options)
