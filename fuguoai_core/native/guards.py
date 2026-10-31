"""统一请求鉴权守卫。

供用户态 API（/api/v1/*）共用：注入一个小程序用户验证器后，各 handler
用 ``api_guard`` 统一守卫，避免各功能各写一套鉴权导致漂移与越权。

本模块只负责「调用验证器 + 统一 401 封装」，不做真正的令牌解析
（那是验证器的职责），也不依赖数据库。
"""

from typing import Callable, Optional, Union

from aiohttp import web

# 模块级全局验证器（单例）。None 表示尚未注入。
_user_verifier: Optional[Callable] = None


def set_user_verifier(fn) -> None:
    """注入小程序用户验证器：fn(request) -> dict | None。

    覆盖已有的验证器。
    """
    global _user_verifier
    _user_verifier = fn


def get_current_user(request) -> Optional[dict]:
    """返回当前请求用户 dict；未注入验证器或验证失败返回 None。"""
    if _user_verifier is None:
        return None
    try:
        return _user_verifier(request)
    except Exception:
        return None


def api_guard(request) -> Union[dict, "web.Response"]:
    """用户态守卫。

    返回 user dict 表示放行；返回 web.Response(401) 表示拒绝。
    验证器未注入时默认 401（fail-closed，宁可拒绝也不放行）。
    """
    user = get_current_user(request)
    if user is None:
        return web.json_response(
            {"code": 401, "message": "未认证或令牌无效"},
            status=401,
            headers={"WWW-Authenticate": 'Bearer realm="fuguoai-user"'},
        )
    return user
