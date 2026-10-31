"""设备天气读取接口（GET /api/v1/weather）。

面向固件（物理 IoT 玩具，有屏幕/灯效），返回精简结构化 JSON 供其渲染，
不走语音链路、不暴露给小程序（小程序走 get_weather LLM 工具）。

鉴权：与 OTA 同语义，设备无小程序 JWT，故【不使用】api_guard。
  - 必须带 `device-id` 请求头（设备身份）；缺失返回 400。
  - server.auth.enabled=false（当前默认）：有 device-id 即放行。
  - server.auth.enabled=true：白名单设备免校验；其余需带
      `client-id` + `Authorization: Bearer <token>`，token 由 AuthManager 校验。

返回（成功）：{"ok": true, "data": {city, icon, desc, temp, high, low, humidity, provider, ts}}
  - icon 为归一化枚举（见 get_weather.NORMALIZED_WEATHER_ICONS），固件只认这组枚举，
    后端在 wttr/qweather 间切换数据源时固件无需改动。

零侵入接入：由 fuguoai_core.api.registry 自动发现并挂载。
"""
import json
import time
import logging

from aiohttp import web

from fuguoai_core.api.registry import register_routes
from fuguoai_core.auth import AuthManager
from fuguoai_core.utils.config_holder import get_config
from fuguoai_plugins.functions.get_weather import (
    get_weather_struct,
    NORMALIZED_WEATHER_ICONS,
)

TAG = "weather_routes"
logger = logging.getLogger(TAG)

# 短 TTL 缓存：固件可能按分钟轮询，避免打爆上游天气源
_STRUCT_CACHE = {}
_STRUCT_TTL = 300  # 秒


def _dumps(o):
    return json.dumps(o, ensure_ascii=False)


def _err(status, message):
    return web.json_response(
        {"code": status, "message": message}, status=status, dumps=_dumps
    )


def _ok(data):
    return web.json_response({"ok": True, "data": data}, dumps=_dumps)


def _extract_bearer(request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip()
    return ""


def _client_ip(request):
    """尽量拿到真实客户端 IP（经反代看 X-Forwarded-For，否则看连接对端）。"""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    peer = request.transport.get_extra_info("peername") if request.transport else None
    if peer:
        return peer[0]
    return request.remote or ""


def _authenticate(request, config):
    """设备鉴权（OTA 同语义）。返回 (device_id, err_response)。"""
    device_id = (request.headers.get("device-id", "") or "").strip()
    if not device_id:
        return None, _err(400, "missing required header: device-id")
    auth_cfg = (config.get("server", {}) or {}).get("auth", {}) or {}
    if not auth_cfg.get("enabled", False):
        return device_id, None  # 当前默认：关闭鉴权，有 device-id 即放行
    allowed = set(auth_cfg.get("allowed_devices", []) or [])
    if allowed and device_id in allowed:
        return device_id, None  # 白名单免校验
    secret_key = (config.get("server", {}) or {}).get("auth_key", "")
    if not secret_key:
        return None, _err(401, "server auth misconfigured")
    expire = auth_cfg.get("expire_seconds")
    auth = AuthManager(secret_key=secret_key, expire_seconds=expire)
    client_id = (request.headers.get("client-id", "") or "").strip()
    token = _extract_bearer(request)
    if not token or not auth.verify_token(token, client_id, device_id):
        return None, _err(401, "invalid or missing token")
    return device_id, None


async def handle_weather(request):
    """GET /api/v1/weather —— 设备只读天气接口。"""
    config = get_config()
    device_id, err = _authenticate(request, config)
    if err:
        return err

    weather_config = (config.get("plugins", {}) or {}).get("get_weather", {}) or {}
    location = request.query.get("location", "").strip() or None
    client_ip = None if location else _client_ip(request)

    # 缓存（按 provider + 最终城市/IP 维度），防轮询打爆上游
    cache_key = "{}|{}|{}".format(
        weather_config.get("provider", "wttr"),
        location or "auto",
        client_ip or "noip",
    )
    now = time.time()
    hit = _STRUCT_CACHE.get(cache_key)
    if hit and (now - hit[0]) < _STRUCT_TTL:
        return _ok(hit[1])

    try:
        data = await get_weather_struct(location, weather_config, client_ip=client_ip)
    except Exception as e:
        logger.error(f"天气接口取数异常 device={device_id}: {e}")
        return _err(502, "weather upstream error")

    if not data:
        return _err(502, "weather unavailable")

    _STRUCT_CACHE[cache_key] = (now, data)
    return _ok(data)


@register_routes
def register(app):
    app.router.add_get("/api/v1/weather", handle_weather)
