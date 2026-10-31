"""M16 · 小程序用户鉴权核心：JWT 签发/校验 + 守卫验证器 + 微信 code2session。

- 复用项目既有 PyJWT（`core/utils/auth.py` 已依赖），HS256 签名。
- 密钥来源（按优先级）：env `FUGUOAI_JWT_SECRET` → config `miniprogram.jwt_secret`
  → 未配置则用临时随机密钥（重启后失效，仅开发期，会告警）。
- `verify_mini_program_user(request)` 即注入 `core.guards` 的验证器：从
  `Authorization: Bearer <jwt>` 解析用户，失败返回 None（守卫据此回 401）。
- 微信登录：小程序 `wx.login()` 拿 code → 本模块 `code2session` 换 openid →
  落库 `users` → 签发 JWT。生产需配置 `miniprogram.appid/secret`；未配置或
  `dev_mode` 开启时走确定性 dev openid，便于联调（生产务必关 dev_mode）。
"""
import asyncio
import hashlib
import json
import logging
import os
import time
import urllib.request

import jwt

TAG = "user_auth"

_WX_CODE2SESSION = "https://api.weixin.qq.com/sns/jscode2session"
_WX_TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"
_WX_PHONE_URL = "https://api.weixin.qq.com/cgi-bin/phone_number/getPhoneNumber"
_SECRET_CACHE = {"v": None}
_CFG_CACHE = {"cfg": None, "ts": 0.0}
_AT_CACHE = {"token": None, "exp": 0.0}  # 微信 access_token 缓存（临近过期刷新）


class WeChatNetworkError(Exception):
    """微信接口网络不可达（连接/超时）→ 应映射为 HTTP 502。

    与业务错误（如 errcode 40029 invalid code）区分：业务错误属客户端
    鉴权失败，应回 400，绝不可回 502（否则客户端误判基础设施故障）。
    """


def _miniprogram_apps(cfg: dict) -> list:
    """返回已配置的小程序列表 [(appid, secret), ...]。

    优先读 config.miniprogram.apps（支持多小程序同时登录，如天使心 +
    复果造物工坊）；否则由顶层 appid/secret（+env 兜底）构造单 app（兼容旧配置）。
    仅收录 appid 与 secret 都非空的条目。
    """
    cfg = cfg or {}
    apps = cfg.get("apps") or []
    result = []
    for a in apps:
        if not isinstance(a, dict):
            continue
        aid = (a.get("appid") or "").strip()
        sec = (a.get("secret") or "").strip()
        if aid and sec:
            result.append((aid, sec))
    if result:
        return result
    # 兼容旧单 app 配置 + env 兜底
    aid = os.environ.get("FUGUOAI_WX_APPID") or cfg.get("appid")
    sec = cfg.get("secret") or os.environ.get("FUGUOAI_WX_SECRET")
    if aid and sec:
        return [(aid, sec)]
    return []


def _miniprogram_config() -> dict:
    """读取 config.yaml 的 miniprogram 段（5s 缓存）。"""
    now = time.time()
    if _CFG_CACHE["cfg"] is not None and now - _CFG_CACHE["ts"] < 5:
        return _CFG_CACHE["cfg"] or {}
    try:
        from fuguoai_config.config_loader import get_project_dir
        import yaml
        with open(get_project_dir() + "config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        _CFG_CACHE["cfg"] = cfg.get("miniprogram") or {}
        _CFG_CACHE["ts"] = now
        return _CFG_CACHE["cfg"]
    except Exception as e:
        logging.getLogger(TAG).warning(f"读取 miniprogram 配置失败: {e}")
        return {}


def _jwt_secret() -> str:
    if _SECRET_CACHE["v"]:
        return _SECRET_CACHE["v"]
    secret = os.environ.get("FUGUOAI_JWT_SECRET")
    if not secret:
        secret = (_miniprogram_config() or {}).get("jwt_secret")
    if not secret:
        secret = os.urandom(24).hex()
        logging.getLogger(TAG).warning(
            "未配置 JWT 密钥，使用临时随机密钥（重启后失效）；"
            "请设置 miniprogram.jwt_secret 或环境变量 FUGUOAI_JWT_SECRET"
        )
    _SECRET_CACHE["v"] = secret
    return secret


def issue_token(user: dict, expires_hours: int = 720) -> str:
    """为用户签发 JWT（默认 30 天有效期）。user 需含 openid / user_id。"""
    payload = {
        "user_id": user.get("user_id"),
        "openid": user.get("openid"),
        "exp": int(time.time()) + expires_hours * 3600,
    }
    return jwt.encode(payload, _jwt_secret(), algorithm="HS256")


def verify_token(token: str):
    """校验 JWT，返回 payload dict 或 None。"""
    if not token:
        return None
    try:
        return jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
    except Exception:
        return None


def verify_mini_program_user(request) -> dict:
    """`core.guards.set_user_verifier` 注入的验证器。

    从 `Authorization: Bearer <jwt>` 解析；无头/非法/过期 → None（守卫回 401）。
    兜底：WS 握手（微信 wx.connectSocket）在升级阶段不转发自定义请求头，
    故允许经查询参数 `?token=<jwt>` 传递（与 web/device-panel/app.js 既有用法一致）。
    两种来源共用同一 JWT 校验，安全性不变。
    """
    auth = request.headers.get("Authorization", "")
    token = ""
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
    if not token:
        token = request.query.get("token", "") or ""
    if not token:
        return None
    return verify_token(token)


def is_auth_dev_mode() -> bool:
    """是否处于鉴权 dev 模式（联调免真实微信）。

    判定与 code2session 完全一致：env `FUGUOAI_AUTH_DEV==1` 或
    config `miniprogram.dev_mode` 为真。**生产必须关闭**，否则可被免密
    签发演示令牌（见 auth_routes.handle_dev_login）。
    """
    cfg = _miniprogram_config() or {}
    return bool(cfg.get("dev_mode")) or os.environ.get("FUGUOAI_AUTH_DEV") == "1"


async def code2session(code: str, appid: str = None) -> dict:
    """微信 jscode2session：code → {openid, session_key, unionid?}。

    支持多小程序：遍历 config.miniprogram.apps，取首个能换到 openid 的 app
    （code 属于哪个 app 就命中哪个），使天使心与复果造物工坊可同时登录。
    dev_mode 或无任何 appid/secret 时返回确定性 dev openid（仅联调）。

    异常区分：
    - WeChatNetworkError：微信接口网络不可达（连接/超时）→ 调用方映射 502。
    - RuntimeError：微信返回业务错误（如 40029 invalid code）→ 调用方映射 400。
    """
    if not code:
        raise ValueError("code 不能为空")
    cfg = _miniprogram_config() or {}
    dev_mode = is_auth_dev_mode()
    apps = _miniprogram_apps(cfg)

    if dev_mode or not apps:
        if dev_mode:
            logging.getLogger(TAG).info("auth dev_mode：使用确定性 dev openid")
        else:
            logging.getLogger(TAG).warning(
                "未配置任何微信 appid/secret 且非 dev_mode，回落 dev openid（生产不可用）"
            )
        openid = "dev_" + hashlib.sha256(code.encode("utf-8")).hexdigest()[:16]
        return {"openid": openid, "dev": True}

    # 客户端可显式带 appid 短路；否则遍历所有已配置小程序，命中能换到 openid 的那个。
    if appid:
        candidates = [(a, s) for (a, s) in apps if a == appid]
    else:
        candidates = list(apps)
    if not candidates:
        candidates = list(apps)

    last_biz = None
    for appid_i, secret_i in candidates:
        url = (
            f"{_WX_CODE2SESSION}?appid={appid_i}&secret={secret_i}"
            f"&js_code={code}&grant_type=authorization_code"
        )

        def _get(url=url):
            with urllib.request.urlopen(url, timeout=8) as resp:
                return json.loads(resp.read().decode("utf-8"))

        try:
            data = await asyncio.to_thread(_get)
        except Exception as e:
            # 网络层失败（连接/超时）→ 微信接口不可达，属基础设施故障 → 502
            raise WeChatNetworkError(f"微信 code2session 网络请求失败: {e}") from e

        if data.get("openid"):
            return data
        # 微信业务错误（如 40029 invalid code）：该 appid 不匹配，试下一个
        last_biz = data

    # 所有已配置 appid 均返回业务错误（code 不属于任何已配置 app，或密钥错）
    raise RuntimeError(f"微信 code2session 业务错误: {last_biz}")


def _wx_appid_secret():
    """读取小程序 appid/secret（取 apps 列表第一个，兼容旧单 app 配置）。

    用于 access_token / 手机号换取等需 appid 级凭据的接口。多小程序场景下
    默认取第一个（天使心）；如需按登录来源区分，可后续扩展为按用户记录 appid。
    """
    apps = _miniprogram_apps(_miniprogram_config() or {})
    if apps:
        return apps[0][0], apps[0][1]
    return None, None


async def get_access_token() -> str:
    """获取微信接口调用凭据 access_token（带缓存，临近过期自动刷新）。

    用于手机号快速验证等需 access_token 的接口。
    """
    now = time.time()
    if _AT_CACHE["token"] and now < _AT_CACHE["exp"] - 300:
        return _AT_CACHE["token"]
    appid, secret = _wx_appid_secret()
    if not (appid and secret):
        raise RuntimeError("未配置微信 appid/secret，无法获取 access_token")
    url = f"{_WX_TOKEN_URL}?grant_type=client_credential&appid={appid}&secret={secret}"

    def _get():
        with urllib.request.urlopen(url, timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        data = await asyncio.to_thread(_get)
    except Exception as e:
        raise RuntimeError(f"获取微信 access_token 失败: {e}") from e
    if "access_token" not in data:
        raise RuntimeError(f"微信 access_token 返回异常: {data}")
    _AT_CACHE["token"] = data["access_token"]
    _AT_CACHE["exp"] = now + float(data.get("expires_in", 7200))
    return _AT_CACHE["token"]


async def get_phone_number(phone_code: str) -> str:
    """微信手机号快速验证：phone_code → 纯手机号（11 位）。

    phone_code 来自小程序 `wx.getPhoneNumber()` 的 code 字段；
    服务端以 access_token 调用 phone_number/getPhoneNumber 换取。
    """
    if not phone_code:
        raise ValueError("phone_code 不能为空")
    token = await get_access_token()
    url = f"{_WX_PHONE_URL}?access_token={token}"

    def _post():
        req = urllib.request.Request(
            url,
            data=json.dumps({"code": phone_code}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        data = await asyncio.to_thread(_post)
    except Exception as e:
        raise RuntimeError(f"微信手机号换取失败: {e}") from e
    if data.get("errcode", 0) != 0:
        raise RuntimeError(f"微信手机号换取失败: {data}")
    phone_info = data.get("phone_info") or {}
    number = phone_info.get("purePhoneNumber") or phone_info.get("phoneNumber") or ""
    if not number:
        raise RuntimeError("微信未返回手机号")
    return number
