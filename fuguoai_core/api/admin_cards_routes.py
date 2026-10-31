"""运营管理端：NFC 实体卡管理 API（/api/v1/admin/cards）。

设计约束（见项目红线）：
- HTTP API 只扩不删改：本文件新增 admin 专用路径，不改动任何既有路由。
- 仅 admin token 可访问；普通请求返回 403。token 取自
  ``$FUGUOAI_ADMIN_TOKEN``，回退兼容 ``$FUGUOAI_VOICE_ADMIN_TOKEN``
  （43 管理后台转发时即使用这两个变量之一，见 43 server.js proxyToGw）。
- 响应信封对齐 43 管理后台前端契约 ``{code, message, data}``：
  - 列表：``data={items:[...], total:N}``（前端取 ``data.items||data.list||data``）。
  - 单卡 / 创建 / 转让：``data`` 为对象。
  - ``code != 0`` 表示失败；HTTP 状态码同步为 400/401/403/404/409。
- 绝不下发 ``card_uid`` 明文（防克隆）；对外一律用 serial 寻址，card_uid 仅在后端
  由 serial 内部解析（get_by_serial），不进入任何响应体。

端点
----
- ``GET  /api/v1/admin/cards``                  全量列表（admin only）
- ``POST /api/v1/admin/cards``                  注册一张卡（serial + character_id + card_uid）
- ``GET  /api/v1/admin/cards/{serial}``         单卡详情（admin only）
- ``POST /api/v1/admin/cards/{serial}/transfer`` 转让 / 强制释放（admin only）
"""

import datetime
import os
import time

from aiohttp import web

from fuguoai_core.api.registry import register_routes
from fuguoai_core.utils.card_registry import get_card_registry


# ------------------------------------------------------------------
# 鉴权 / CORS / 信封（与 admin_devices_routes.py 同范式）
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


def _strip_uid(card: dict) -> dict:
    """去掉 card_uid 明文，避免随响应下发。"""
    return {k: v for k, v in card.items() if k != "card_uid"}


_BJ = datetime.timezone(datetime.timedelta(hours=8))
_TIME_FIELDS = ("created_at", "claimed_at", "transferred_at", "wiped_at")


def _iso_time(ts) -> str:
    """把 float/int 时间戳或日期字符串统一转成 ``YYYY-MM-DD`` 字符串。

    注册表里时间字段混合：seed 数据有 ``"2026-10-10"`` 字符串，claim 后写入的是
    float 时间戳。43 前端对 created_at 做 split/格式化时要求 ``YYYY-MM-DD``
    形态（带 ``T``/时区的 ISO 字符串会被解析成 NaN）。这里统一成日期字符串输出。
    """
    if ts is None:
        return ""
    if isinstance(ts, str):
        ts = ts.strip()
        if not ts:
            return ""
        # 兼容纯数字时间戳字符串（如 "1791942823.777"）
        try:
            return datetime.datetime.fromtimestamp(float(ts), tz=_BJ).strftime("%Y-%m-%d")
        except (ValueError, TypeError, OverflowError, OSError):
            return ts  # 已经是日期字符串，原样返回
    if isinstance(ts, (int, float)):
        try:
            return datetime.datetime.fromtimestamp(float(ts), tz=_BJ).strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            return str(ts)
    return str(ts)


def _format_card(card: dict) -> dict:
    """把卡记录里的时间字段统一格式化为字符串，其余字段原样保留。"""
    d = dict(card)
    for k in _TIME_FIELDS:
        if k in d:
            d[k] = _iso_time(d[k])
    return d


# ------------------------------------------------------------------
# 处理器
# ------------------------------------------------------------------
async def handle_list_cards(request: web.Request):
    """GET /api/v1/admin/cards —— 全量卡列表（admin only）。"""
    if not _is_admin(request):
        return _err(403, "admin token required", 403)
    try:
        items = [_format_card(_strip_uid(c)) for c in get_card_registry().list_all()]
        return _ok({"items": items, "total": len(items)})
    except Exception as e:  # 绝不外溢
        return _err(500, f"list failed: {e}", 500)


async def handle_create_card(request: web.Request):
    """POST /api/v1/admin/cards —— 注册/建绑一张卡。

    body: ``{serial, character_id, card_uid?}``（card_uid 必填：注册表以 card_uid 为主键，
    无物理卡 UID 无法落库）。重复 card_uid / serial 返回 409。
    """
    if not _is_admin(request):
        return _err(403, "admin token required", 403)
    body = await _json_body(request)
    serial = (body.get("serial") or "").strip()
    character_id = (body.get("character_id") or "").strip()
    card_uid = (body.get("card_uid") or "").strip()
    if not serial:
        return _err(400, "serial 必填（卡面编号，如 xiaoman-001）", 400)
    if not character_id:
        return _err(400, "character_id 必填（角色 id，如 xiaoman/ache/zhiqui/dengdeng）", 400)
    if not card_uid:
        return _err(400, "card_uid 必填（需用读卡器读取物理卡 UID 后填入，注册表以它为主键）", 400)

    reg = get_card_registry()
    if reg.get(card_uid):
        return _err(409, "card_uid 已注册，请勿重复创建（如需换绑角色请联系后端）", 409)
    for c in reg.list_all():
        if (c.get("serial") or "").strip() == serial:
            return _err(409, f"编号 {serial} 已被占用", 409)

    res = reg.bind(card_uid, character_id, serial)
    if not res.get("ok"):
        return _err(400, f"bind 失败: {res.get('reason')}", 400)
    return _ok(_format_card(_strip_uid(res.get("binding", {}))), "created")


async def handle_get_card(request: web.Request):
    """GET /api/v1/admin/cards/{serial} —— 单卡详情（admin only）。"""
    if not _is_admin(request):
        return _err(403, "admin token required", 403)
    serial = (request.match_info.get("serial") or "").strip()
    cuid, card = get_card_registry().get_by_serial(serial)
    if not card:
        return _err(404, f"未找到编号 {serial} 的卡", 404)
    d = _format_card(_strip_uid(card))
    d["serial"] = serial
    return _ok(d)


async def handle_transfer_card(request: web.Request):
    """POST /api/v1/admin/cards/{serial}/transfer —— 转让/强制释放（admin only）。

    空 body。找到卡后清空 owner_uid（admin_release），保留 lineage、置 transferred_at，
    等下一任碰卡 claim 重新绑定自己。无持有者则 409。
    """
    if not _is_admin(request):
        return _err(403, "admin token required", 403)
    serial = (request.match_info.get("serial") or "").strip()
    reg = get_card_registry()
    cuid, card = reg.get_by_serial(serial)
    if not card:
        return _err(404, f"未找到编号 {serial} 的卡", 404)
    if not card.get("owner_uid"):
        return _err(409, f"编号 {serial} 当前无持有者，无需转让", 409)
    res = reg.admin_release(cuid)
    if not res.get("ok"):
        return _err(400, f"release 失败: {res.get('reason')}", 400)
    return _ok({
        "serial": serial,
        "character_id": res.get("character_id"),
        "previous_owner": res.get("previous_owner"),
        "transferred_at": _iso_time(res.get("transferred_at")),
    }, "transferred")


async def handle_options(request: web.Request):
    return _cors(web.Response(status=204))


@register_routes
def register(app):
    app.router.add_get("/api/v1/admin/cards", handle_list_cards)
    app.router.add_post("/api/v1/admin/cards", handle_create_card)
    app.router.add_get("/api/v1/admin/cards/{serial}", handle_get_card)
    app.router.add_post("/api/v1/admin/cards/{serial}/transfer", handle_transfer_card)
    app.router.add_options("/api/v1/admin/cards", handle_options)
    app.router.add_options("/api/v1/admin/cards/{serial}", handle_options)
    app.router.add_options("/api/v1/admin/cards/{serial}/transfer", handle_options)
