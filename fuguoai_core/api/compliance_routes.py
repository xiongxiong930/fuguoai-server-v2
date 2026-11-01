"""复果合规层 · 管理 API（/api/v1/admin/compliance）。

合规层（#146 示范）：对外暴露三类能力
1. ``GET  /api/v1/admin/compliance/status``   当前合规开关快照 + 审计统计；
2. ``POST /api/v1/admin/compliance/set``       运行时热切换开关（仅改内存单例，持久化由运营改 data/.config.yaml）；
                                              每次切换写审计（category=config）；
3. ``POST /api/v1/admin/compliance/audit/query`` 按过滤条件查审计日志；
4. ``GET  /api/v1/admin/compliance/audit/export`` 导出审计日志（JSON）。

**录音不出域红线（recording_no_egress）**：导出接口若处于该红线之下，凡
``category='recording'`` 的审计记录一律不出域——被剥离并在响应中报告
``redacted_recording_events`` 计数，满足「录音不出域」的可执行约束。

鉴权 / CORS / 信封：复用 admin 令牌（FUGUOAI_ADMIN_TOKEN / FUGUOAI_VOICE_ADMIN_TOKEN 回退），
与 brand_kb_routes.py / admin_cards_routes.py 同范式；响应信封对齐 43 管理后台 {code,message,data}。

自动挂载：文件名以 ``_routes.py`` 结尾，由 registry.apply_routes 按命名约定自动发现。
"""

import json

from aiohttp import web

from fuguoai_config.logger import setup_logging
from fuguoai_core.api.registry import register_routes
from fuguoai_core.utils import compliance as cmp_mod

logger = setup_logging()


# ------------------------------------------------------------------
# 鉴权 / CORS / 信封（与 admin_cards_routes.py 同范式）
# ------------------------------------------------------------------
def _is_admin(request) -> bool:
    import os
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
    try:
        return await request.json() or {}
    except Exception:
        return {}


# ------------------------------------------------------------------
# 路由注册（自动发现挂载）
# ------------------------------------------------------------------
@register_routes
def register_compliance_routes(app):
    app.router.add_get("/api/v1/admin/compliance/status", _handle_status)
    app.router.add_post("/api/v1/admin/compliance/set", _handle_set)
    app.router.add_post("/api/v1/admin/compliance/audit/query", _handle_audit_query)
    app.router.add_get("/api/v1/admin/compliance/audit/export", _handle_audit_export)
    app.router.add_options("/api/v1/admin/compliance/{tail:.*}", _handle_options)


async def _handle_options(request):
    return _cors(web.Response())


# ------------------------------------------------------------------
# 处理器
# ------------------------------------------------------------------
async def _handle_status(request: web.Request):
    """GET /api/v1/admin/compliance/status —— 开关快照 + 审计统计。"""
    if not _is_admin(request):
        return _err(403, "admin token required", 403)
    cfg = cmp_mod.get_compliance()
    store = cmp_mod.get_audit_store()
    # 按 category 聚合审计条数
    try:
        conn = store._connect()
        try:
            conn.row_factory = None
            rows = conn.execute(
                "SELECT category, COUNT(*) FROM audit_log GROUP BY category"
            ).fetchall()
            by_category = {r[0]: r[1] for r in rows}
        finally:
            conn.close()
    except Exception:
        by_category = {}
    snap = cfg.snapshot()
    snap["audit_total"] = store.count()
    snap["audit_by_category"] = by_category
    return _ok(snap)


async def _handle_set(request: web.Request):
    """POST /api/v1/admin/compliance/set —— 运行时热切换合规开关。

    body 可含：child_mode_enabled / recording_no_egress / physical_mute_enforced /
    encrypt_at_rest（bool）。仅写内存单例，并写一条 category=config 的审计。
    持久化（重启生效）由运营修改 data/.config.yaml 完成。
    """
    if not _is_admin(request):
        return _err(403, "admin token required", 403)
    body = await _json_body(request)
    cfg = cmp_mod.get_compliance()
    actor = (body.get("actor") or "admin")
    changed = {}
    bool_fields = {
        "enabled": "enabled",
        "child_mode_enabled": "child_mode_enabled",
        "recording_no_egress": "recording_no_egress",
        "physical_mute_enforced": "physical_mute_enforced",
        "encrypt_at_rest": "encrypt_at_rest",
    }
    for key, attr in bool_fields.items():
        if key in body and isinstance(body[key], bool):
            setattr(cfg, attr, body[key])
            changed[key] = body[key]
    # 记审计（配置变更始终可溯）
    cmp_mod.record_audit(
        event_type="compliance_config_change",
        actor=actor,
        detail={"changed": changed, "snapshot": cfg.snapshot()},
        result="allow" if changed else "noop",
        category="config",
    )
    return _ok({"changed": changed, "snapshot": cfg.snapshot()}, "compliance switches updated")


async def _handle_audit_query(request: web.Request):
    """POST /api/v1/admin/compliance/audit/query —— 查审计日志。

    body 可含：event_type / actor / tenant_id / since(unix) / limit。
    """
    if not _is_admin(request):
        return _err(403, "admin token required", 403)
    body = await _json_body(request)
    store = cmp_mod.get_audit_store()
    rows = store.query(
        event_type=(body.get("event_type") or "").strip() or None,
        actor=(body.get("actor") or "").strip() or None,
        tenant_id=body.get("tenant_id") if body.get("tenant_id") is not None else None,
        since=body.get("since"),
        limit=body.get("limit") or 200,
    )
    return _ok({"items": rows, "total": len(rows)})


async def _handle_audit_export(request: web.Request):
    """GET /api/v1/admin/compliance/audit/export —— 导出审计日志（JSON）。

    红线：若 recording_no_egress 开启，category='recording' 的记录不出域——
    被剥离并在响应报告 redacted_recording_events 计数。
    其余记录照常导出（审计导出自身属管理行为，受 admin 鉴权保护）。
    """
    if not _is_admin(request):
        return _err(403, "admin token required", 403)
    cfg = cmp_mod.get_compliance()
    store = cmp_mod.get_audit_store()
    rows = store.query(limit=10_000)
    redacted = 0
    if cfg.is_recording_egress_blocked():
        kept = []
        for r in rows:
            if r.get("category") == "recording":
                redacted += 1
                continue
            kept.append(r)
        rows = kept
    cmp_mod.record_audit(
        event_type="compliance_audit_export",
        actor="admin",
        detail={"exported": len(rows), "redacted_recording": redacted,
                "egress_blocked": cfg.is_recording_egress_blocked()},
        result="allow",
        category="access",
    )
    return _ok({
        "items": rows,
        "total": len(rows),
        "redacted_recording_events": redacted,
        "recording_egress_blocked": cfg.is_recording_egress_blocked(),
    }, "audit exported")
