"""M16 · 小程序登录与用户态路由（/api/v1/auth）。

零侵入接入：本文件由 fuguoai_core.api.registry 自动发现并挂载，不改动 http_server 既有路由。
- `POST /api/v1/auth/login`：公开接口（无需令牌）。小程序 `wx.login()` 拿 code 调此，
  后端 code2session 换 openid → 落库 users → 签发 JWT 返回。
- `GET  /api/v1/auth/me`：守卫接口，返回当前 JWT 对应的用户信息（验证守卫 200 路径用）。
- `POST /api/v1/auth/dev-login`：演示专用免微信登录端点（dev 模式生效）。直接为
  固定演示用户签发 JWT，返回 `{code:0, data:{token}}`；非 dev 模式返回 403 fail-closed。

模块导入时即 `set_user_verifier(verify_mini_program_user)`，解锁所有 /api/v1/* 守卫
（此前一律 401 fail-closed）。若本模块导入失败，守卫保持 401（安全默认）。
"""
import json
import logging

from aiohttp import web

from fuguoai_core.api.registry import register_routes
from fuguoai_core.guards import api_guard, set_user_verifier
from fuguoai_core.utils.user_auth import (
    code2session,
    issue_token,
    verify_mini_program_user,
    get_phone_number,
    is_auth_dev_mode,
)
from fuguoai_core.utils.user_store import get_user_store

TAG = "auth_routes"
logger = logging.getLogger(TAG)

# 启动即注入验证器 → 解锁用户态守卫（M16 上线）
set_user_verifier(verify_mini_program_user)


def _dumps(o):
    return json.dumps(o, ensure_ascii=False)


def _add_cors(request, resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "content-type, authorization"
    return resp


def _err(status, message):
    return web.json_response({"code": status, "message": message}, status=status)


@register_routes
def register(app):
    app.router.add_post("/api/v1/auth/login", handle_login)
    app.router.add_post("/api/v1/auth/dev-login", handle_dev_login)
    app.router.add_get("/api/v1/auth/me", handle_me)
    app.router.add_options("/api/v1/auth/login", handle_options)
    app.router.add_options("/api/v1/auth/dev-login", handle_options)
    app.router.add_options("/api/v1/auth/me", handle_options)


async def handle_options(request):
    return _add_cors(request, web.Response(text=""))


async def handle_login(request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return _add_cors(request, _err(400, "请求体应为 JSON 对象"))
    code = (payload.get("code") or "").strip()
    if not code:
        return _add_cors(request, _err(400, "缺少 wx.login 的 code"))

    # 1) 微信 code → openid
    try:
        wx = await code2session(code)
    except Exception as e:
        logger.warning(f"code2session 失败: {e}")
        msg = str(e)
        if "网络请求失败" in msg:
            # 微信接口网络异常（瞬时段错误），属服务端上游问题 → 502
            return _add_cors(request, _err(502, f"微信登录校验失败（网络异常，请稍后重试）: {e}"))
        # 微信返回业务错误（如 errcode 40029 invalid code / 401 等）：属客户端鉴权失败 → 400
        # 注意：绝不能回 502，否则客户端误判成基础设施故障、重试无效。
        return _add_cors(request, _err(400, f"微信登录校验失败: {e}"))

    openid = wx.get("openid")
    if not openid:
        return _add_cors(request, _err(400, "微信未返回 openid"))

    # 1.5) 手机号快速验证（可选）：phone_code → 绑定手机号
    phone = None
    phone_code = (payload.get("phone_code") or "").strip()
    if phone_code:
        try:
            phone = await get_phone_number(phone_code)
        except Exception as e:
            # 手机号换取失败不影响登录（降级为不绑定手机号）
            logger.warning(f"phone_code 换取手机号失败（不影响登录）: {e}")
            phone = None

    # 2) 落库用户（昵称/头像/手机号可选，小程序可后续上传）
    nickname = payload.get("nickname")
    avatar = payload.get("avatar")
    try:
        user = get_user_store().upsert(openid, nickname=nickname, avatar=avatar, phone=phone)
    except Exception as e:
        logger.error(f"用户落库失败: {e}")
        return _add_cors(request, _err(500, "用户存储失败"))

    # 3) 签发 JWT
    token = issue_token(user)
    return _add_cors(request, web.json_response(
        {
            "token": token,
            "user": {
                "user_id": user.get("user_id"),
                "openid": user.get("openid"),
                "nickname": user.get("nickname"),
                "avatar": user.get("avatar"),
                "phone": user.get("phone"),
            },
        },
        dumps=_dumps,
    ))


# 演示登录固定身份：稳定 openid → 同一 user_id，使记忆/对话演示可复现
DEV_DEMO_OPENID = "dev_angel_heart_demo"
DEV_DEMO_NICKNAME = "演示用户"


async def handle_dev_login(request):
    """演示专用免微信登录端点（仅 dev 模式生效）。

    契约文档 §7：不调 code2session，直接为固定演示用户签发 JWT，
    返回 `{code:0, data:{token}}`（与前端 api.request 解包 body.data 一致）。
    非 dev 模式（生产）一律 403 fail-closed，杜绝免密登录暴露。
    """
    if not is_auth_dev_mode():
        return _add_cors(
            request,
            _err(403, "dev-login 仅在 dev 模式可用（生产已禁用）"),
        )

    # 固定演示用户落库 → 稳定 user_id，便于记忆/对话演示复现
    try:
        user = get_user_store().upsert(
            DEV_DEMO_OPENID, nickname=DEV_DEMO_NICKNAME, avatar=None, phone=None
        )
    except Exception as e:
        logger.error(f"dev-login 用户落库失败: {e}")
        return _add_cors(request, _err(500, "用户存储失败"))

    token = issue_token(user)
    return _add_cors(
        request,
        web.json_response({"code": 0, "data": {"token": token}}, dumps=_dumps),
    )


async def handle_me(request):
    user = api_guard(request)
    if isinstance(user, web.Response):
        return _add_cors(request, user)
    # user 为 JWT payload（含 user_id/openid）
    store = get_user_store()
    full = store.get_by_user_id(user.get("user_id")) or store.get_by_openid(user.get("openid")) or {}
    return _add_cors(request, web.json_response(
        {
            "user_id": user.get("user_id"),
            "openid": user.get("openid"),
            "nickname": full.get("nickname"),
            "avatar": full.get("avatar"),
            "phone": full.get("phone"),
        },
        dumps=_dumps,
    ))
