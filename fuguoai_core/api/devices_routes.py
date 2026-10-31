"""配网设备绑定 API（/api/v1/devices）。

零侵入接入：本文件由 fuguoai_core.api.registry 自动发现并挂载，不改动 http_server 既有路由。

接口（前缀 /api/v1，feature=devices）
- POST /api/v1/devices/bind-token   [用户态守卫] 小程序领取一次性 bind_token（配网下发用）
- POST /api/v1/devices/bind         [bind_token 鉴权] ESP32 配网成功后上报绑定；写 device_binding
- POST /api/v1/devices/bind-by-code [用户态守卫] 小程序输入 6 位绑定码认领设备（AP 配网范式二，路2 manage 权威，Python 仅薄转发）
- GET  /api/v1/devices              [用户态守卫] 列出当前用户名下已绑定设备；响应体 `{"ok": true, "data": [设备对象]}`（前端 listDevices 解包 body.data）。设备对象含 today（今日用量：sessions/turns/response_chars/duration_seconds）+ weekly（近7日累计，同字段 + last_active_ts）+ online/last_active
- GET  /api/v1/devices/{device_id}/usage   [用户态守卫] 该设备近 days 天用量汇总（?days=，默认7，上限365），需本人归属
- GET  /api/v1/devices/{device_id}/memory  [用户态守卫] 该设备互动记忆「安全派生摘要」（?detail=true 追加高光语录/记忆立方明细），需本人归属
- POST /api/v1/devices/unbind       [用户态守卫] 解绑设备（status→unbound）
- OPTIONS *                          CORS 预检
- GET  /api/v1/devices/{device_id}/reminders        [用户态守卫] 小程序「我的提醒」统一视图（设备上行镜像 + 云端在线日程合并）
- GET  /api/v1/devices/{device_id}/schedules/cloud  [用户态守卫] 列出该设备云端在线日程（id 高位区间，后端真源）
- POST /api/v1/devices/{device_id}/schedules/cloud  [用户态守卫] 创建云端在线日程（分配高位 id，实时下行设备 + 广播事件）
- PUT  /api/v1/devices/{device_id}/schedules/cloud/{task_id}  [用户态守卫] 更新云端在线日程
- DELETE /api/v1/devices/{device_id}/schedules/cloud/{task_id} [用户态守卫] 删除云端在线日程（下发 tombstone）

鉴权说明
--------
- bind-token / list / unbind 走 core.guards.api_guard（小程序 JWT），符合 §八 统一守卫约定。
- bind 由 ESP32 调用，无用户 JWT；以 bind_token 作为「设备侧」鉴权凭证（类比 M7 设备令牌），
  由本模块独立校验。这不属于「自造用户鉴权」——用户态守卫本就无法覆盖无 JWT 的设备直连场景。
  ESP32 契约（见 esp32-provision-fw/main/app_main.c::http_bind_device）：
    POST, Content-Type: application/json,
    body {"device_id": "...", "bind_token": "..."},
    另 Authorization 头也带「裸」bind_token（无 Bearer 前缀），本模块做兜底解析。
"""
import hashlib
import json
import logging
import os
import time

import yaml
from aiohttp import web

from fuguoai_core.api.registry import register_routes
from fuguoai_core.guards import api_guard
from fuguoai_core.device_access import DeviceBindingStore
from fuguoai_core.utils.util import normalize_device_id
from fuguoai_core.utils.bind_token_store import get_bind_token_store
from fuguoai_core.utils.connection_registry import get_connection_registry
from fuguoai_core.utils.usage_store import get_usage_store
from fuguoai_core.utils.metrics import METRICS
from fuguoai_config.manage_api_client import (
    bind_by_code as manage_bind_by_code,
    DeviceBindException,
    DeviceNotFoundException,
)

TAG = "devices_routes"
logger = logging.getLogger(TAG)

_BIND_TOKEN_TTL = 600  # 10 分钟


def _dumps(o):
    return json.dumps(o, ensure_ascii=False)


def _add_cors(request, resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "content-type, authorization"
    return resp


def _err(status, message):
    return web.json_response({"code": status, "message": message}, status=status)


def _user_id(user: dict) -> str:
    return (user or {}).get("user_id") or (user or {}).get("openid")


def _get_owned_device(binding, uid: str, raw_device_id: str):
    """大小写/形态归一化匹配「用户名下」设备，返回精确存储 id 的记录；非本人或无匹配返回 None。

    调用方统一用 404 处理 None，不向非所有者泄露设备存在性。
    """
    did = normalize_device_id(raw_device_id)
    for d in binding.list_all():
        if normalize_device_id(d.get("device_id", "")) == did:
            if d.get("user_id") == uid:
                return d
            return None  # 命中但非本人，按无权限处理
    return None


_DEV_MODE_CACHE = None
_DEV_MODE_CACHE_AT = 0.0
_DEV_MODE_TTL = 300.0  # 5 分钟缓存，避免每次绑定请求同步重读 config.yaml 卡事件循环


def _dev_mode() -> bool:
    """联调兜底开关：miniprogram.dev_mode 或 env FUGUOAI_AUTH_DEV=1。生产务必关闭。

    结果缓存 5 分钟（dev_mode 是联调开关、生产常关，几乎不变），
    避免每次配网绑定请求都同步 open+safe_load 卡住 aiohttp 事件循环。
    """
    global _DEV_MODE_CACHE, _DEV_MODE_CACHE_AT
    now = time.time()
    if _DEV_MODE_CACHE is not None and now - _DEV_MODE_CACHE_AT < _DEV_MODE_TTL:
        return _DEV_MODE_CACHE
    try:
        from fuguoai_config.config_loader import get_project_dir
        with open(get_project_dir() + "config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        mp = cfg.get("miniprogram") or {}
        val = bool(mp.get("dev_mode")) or os.environ.get("FUGUOAI_AUTH_DEV") == "1"
    except Exception:
        val = False
    _DEV_MODE_CACHE = val
    _DEV_MODE_CACHE_AT = now
    return val


def _summarize_memory(raw: str, mtime: int = None, detail: bool = False) -> dict:
    """从原始记忆文本派生安全摘要（不暴露 AI 内部结构与用户原话全文）。

    返回字段固定，便于小程序设备管理稳定解析：
    - has_memory: 是否有记忆
    - memory_updated_at: 记忆文件最后修改时间（整文件共享，仅作近似新鲜度参考）
    - memory_chars: 记忆体量（字符数）
    - name: AI 记住的称呼（时空档案.身份图谱.现用名）
    - topics: 高频话题标签（关系网络.高频话题 的键，最多20）
    - highlights_count: 高光语录条数
    - cube_count: 记忆立方条目数

    detail=True 时追加：
    - highlights: 高光语录字符串数组（用户原话，最多50条）
    - cubes: 记忆立方对象数组（每条含 事件/时间戳/情感值/关联项/保鲜期，最多30条）
    """
    base = {
        "has_memory": False,
        "memory_updated_at": mtime,
        "memory_chars": 0,
        "name": None,
        "topics": [],
        "highlights_count": 0,
        "cube_count": 0,
    }
    if not raw or not raw.strip():
        return base
    text = raw.strip()
    base["memory_chars"] = len(text)
    base["has_memory"] = True
    # 兼容 ```json 围栏包裹
    js = text
    try:
        if "```json" in js:
            s = js.find("```json") + 7
            e = js.find("```", s)
            js = js[s:e] if e != -1 else js
        data = json.loads(js)
    except Exception:
        data = None
    if isinstance(data, dict):
        profile = data.get("时空档案") or {}
        if isinstance(profile, dict):
            base["name"] = (profile.get("身份图谱") or {}).get("现用名") or None
            cube = profile.get("记忆立方") or []
            if isinstance(cube, list):
                base["cube_count"] = len(cube)
                if detail:
                    # 记忆立方：提取结构化字段，脱敏截断
                    base["cubes"] = [
                        {
                            "event": str(c.get("事件", ""))[:200],
                            "timestamp": c.get("时间戳", ""),
                            "emotion": float(c.get("情感值", 0)) if isinstance(c.get("情感值"), (int, float)) else None,
                            "associations": (c.get("关联项") or [])[:10],
                        }
                        for c in cube[:30]
                    ]
        rel = data.get("关系网络") or {}
        if isinstance(rel, dict):
            topics = rel.get("高频话题") or {}
            if isinstance(topics, dict):
                base["topics"] = [str(k) for k in topics.keys()][:20]
        hl = data.get("高光语录") or []
        if isinstance(hl, list):
            base["highlights_count"] = len(hl)
            if detail:
                base["highlights"] = [str(h)[:500] for h in hl[:50]]
    return base


@register_routes
def register(app):
    app.router.add_post("/api/v1/devices/bind-token", handle_bind_token)
    app.router.add_post("/api/v1/devices/bind", handle_bind)
    app.router.add_post("/api/v1/devices/bind-by-code", handle_bind_by_code)
    app.router.add_post("/api/v1/devices/unbind", handle_unbind)
    app.router.add_get("/api/v1/devices", handle_list)
    app.router.add_options("/api/v1/devices", handle_options)
    app.router.add_get("/api/v1/devices/{device_id}/usage", handle_device_usage)
    app.router.add_get("/api/v1/devices/{device_id}/memory", handle_device_memory)
    app.router.add_options("/api/v1/devices/{device_id}/usage", handle_options)
    app.router.add_options("/api/v1/devices/{device_id}/memory", handle_options)
    app.router.add_options("/api/v1/devices/bind-token", handle_options)
    app.router.add_options("/api/v1/devices/bind", handle_options)
    app.router.add_options("/api/v1/devices/bind-by-code", handle_options)
    app.router.add_options("/api/v1/devices/unbind", handle_options)
    # 物理删除设备（仅删自己名下 unbound）；device_id 取 URL 原始大小写
    app.router.add_delete("/api/v1/devices/{device_id}", handle_delete_device)
    app.router.add_options("/api/v1/devices/{device_id}", handle_options)
    # 对讲会话控制（契约 v1.6.0 Part B，只扩不改）
    app.router.add_post("/api/v1/devices/{device_id}/intercom", handle_intercom)
    app.router.add_options("/api/v1/devices/{device_id}/intercom", handle_options)
    # 小程序日程管理（对接文档 §8，只扩不改）
    app.router.add_post("/api/v1/devices/{device_id}/command", handle_device_command)
    app.router.add_options("/api/v1/devices/{device_id}/command", handle_options)
    app.router.add_get("/api/v1/devices/{device_id}/reminders", handle_device_reminders)
    app.router.add_options("/api/v1/devices/{device_id}/reminders", handle_options)
    # M2 云端在线日程 CRUD（小程序创建/管理，下行同步到设备，只扩不改）
    app.router.add_get("/api/v1/devices/{device_id}/schedules/cloud", handle_list_cloud_schedule)
    app.router.add_post("/api/v1/devices/{device_id}/schedules/cloud", handle_create_cloud_schedule)
    app.router.add_put("/api/v1/devices/{device_id}/schedules/cloud/{task_id}", handle_update_cloud_schedule)
    app.router.add_delete("/api/v1/devices/{device_id}/schedules/cloud/{task_id}", handle_delete_cloud_schedule)
    app.router.add_options("/api/v1/devices/{device_id}/schedules/cloud", handle_options)
    app.router.add_options("/api/v1/devices/{device_id}/schedules/cloud/{task_id}", handle_options)


async def handle_options(request):
    return _add_cors(request, web.Response(text=""))


# ─────────────────────────────────────────────────────────
# 小程序：领取一次性 bind_token（配网下发用）
# ─────────────────────────────────────────────────────────
async def handle_bind_token(request):
    user = api_guard(request)
    if isinstance(user, web.Response):
        return _add_cors(request, user)
    uid = _user_id(user)
    if not uid:
        return _add_cors(request, _err(400, "无法确定当前用户"))
    try:
        res = get_bind_token_store().issue(uid, ttl=_BIND_TOKEN_TTL)
    except Exception as e:
        logger.error(f"签发 bind_token 失败: {e}")
        return _add_cors(request, _err(500, "签发绑定令牌失败"))
    return _add_cors(request, web.json_response(
        {
            "bind_token": res["bind_token"],
            "expires_in": res["expires_in"],
            "expires_at": res["expires_at"],
        },
        dumps=_dumps,
    ))


# ─────────────────────────────────────────────────────────
# ESP32：配网成功后上报绑定（bind_token 鉴权）
# ─────────────────────────────────────────────────────────
async def handle_bind(request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return _add_cors(request, _err(400, "请求体应为 JSON 对象"))

    device_id = str(payload.get("device_id") or "").strip()
    # 归一到规范形式（小写+去冒号/连字符），与 WS 侧 connection.py 的 device_id 取
    # 法保持一致，避免同一块板因「WS 用小写 / BLE-bind 用大写」裂成两行。
    device_id = normalize_device_id(device_id)
    token = str(payload.get("bind_token") or "").strip()
    if not token:
        # 兜底：固件把 bind_token 放在 Authorization 头（裸串，无 Bearer 前缀）
        auth = request.headers.get("Authorization", "").strip()
        if auth.lower().startswith("bearer "):
            auth = auth[7:].strip()
        token = auth

    if not device_id:
        return _add_cors(request, _err(400, "缺少 device_id"))
    if not token:
        return _add_cors(request, _err(401, "缺少 bind_token"))

    store = get_bind_token_store()
    rec = store.get(token)
    now = int(time.time())
    binding = DeviceBindingStore()

    # 联调兜底：dev_mode 下即便 token 未签发也放行，绑到确定性 dev 用户，便于固件+后端先行联调
    if rec is None:
        if _dev_mode():
            dev_uid = "dev_bind_" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
            binding.set_status(device_id, "active", user_id=dev_uid)
            dtoken = binding.issue_device_token(device_id)
            logger.error(f"dev_mode：未签发 token 直接绑定 device={device_id} → {dev_uid}（仅限联调，生产须关闭 dev_mode）")
            # 加固：dev_mode 绑定绕过可被观测。生产(dev_mode=false)永不触发；一旦触发即说明联调开关被打开，需人工确认。
            try:
                METRICS.inc("fuguoai_dev_mode_bind_bypass_total", labels={"device_id": device_id or "unknown"})
            except Exception:
                pass
            resp = {"ok": True, "device_id": device_id, "user_id": dev_uid,
                    "status": "active", "dev": True}
            if dtoken:
                resp["device_token"] = dtoken
            return _add_cors(request, web.json_response(resp, dumps=_dumps))
        return _add_cors(request, _err(401, "bind_token 无效"))

    if rec.get("expires_at") and now > rec["expires_at"]:
        return _add_cors(request, _err(401, "bind_token 已过期，请在小程序重新发起配网"))

    if rec.get("used_at"):
        # 一次性令牌已用：同设备幂等放行，异设备拒绝
        if rec.get("device_id") == device_id:
            dtoken = binding.get_device_token(device_id)
            resp = {"ok": True, "device_id": device_id, "user_id": rec["user_id"],
                    "status": "active", "idempotent": True}
            if dtoken:
                resp["device_token"] = dtoken
            return _add_cors(request, web.json_response(resp, dumps=_dumps))
        return _add_cors(request, _err(409, "bind_token 已被其他设备使用"))

    # 正常绑定
    binding.set_status(device_id, "active", user_id=rec["user_id"])
    store.mark_used(token, device_id)
    dtoken = binding.issue_device_token(device_id)
    logger.info(f"设备绑定成功 device={device_id} → user={rec['user_id']}")
    resp = {"ok": True, "device_id": device_id, "user_id": rec["user_id"], "status": "active"}
    if dtoken:
        resp["device_token"] = dtoken
    return _add_cors(request, web.json_response(resp, dumps=_dumps))


# ─────────────────────────────────────────────────────────
# 小程序：用户输入 6 位绑定码认领设备（AP 配网范式二，路2 manage 权威）
# ─────────────────────────────────────────────────────────
async def handle_bind_by_code(request):
    user = api_guard(request)
    if isinstance(user, web.Response):
        return _add_cors(request, user)
    uid = _user_id(user)
    if not uid:
        return _add_cors(request, _err(400, "无法确定当前用户"))
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return _add_cors(request, _err(400, "请求体应为 JSON 对象"))
    code = str((payload or {}).get("code") or "").strip()
    if not code:
        return _add_cors(request, _err(400, "缺少绑定码 code"))

    # 薄转发到 manage 权威绑定：manage 写绑定并返回 deviceId；Python 侧不做本地绑定写入（无双写）。
    try:
        result = await manage_bind_by_code(code, uid)
    except DeviceBindException:
        return _add_cors(request, _err(409, "绑定码无效或已失效，请确认后重试"))
    except DeviceNotFoundException:
        return _add_cors(request, _err(404, "绑定码对应的设备不存在"))
    except Exception as e:
        logger.error(f"bind_by_code 转发 manage 失败: {e}")
        return _add_cors(request, _err(502, "绑定服务暂不可用，请稍后重试"))

    if not result or not result.get("deviceId"):
        return _add_cors(request, _err(502, "绑定服务返回异常"))
    device_id = str(result["deviceId"]).strip()

    # 双链路推送 device.bound：
    # ① 推小程序（/api/v1/events）：列表刷新（跨线程安全，对齐 device.online 结构）
    try:
        from fuguoai_core.utils.event_bus import get_event_bus
        get_event_bus().publish({
            "type": "device.bound",
            "device_id": device_id,
            "user_id": uid,
            "ts": int(time.time()),
        })
    except Exception:
        pass
    # ② 推设备 WS：清 need_bind（经 connection.safe_send 串行下发，避免并发帧错乱）
    try:
        reg = get_connection_registry()
        sent = await reg.send_to_device(
            device_id,
            {"type": "device.bound", "device_id": device_id, "status": "active", "ts": int(time.time())},
        )
        if not sent:
            logger.info(
                f"bind_by_code: 设备未在线/无需推送 WS(device_id={device_id})，"
                f"固件将在下次拉配置时自动清 need_bind"
            )
    except Exception as e:
        logger.warning(f"推送 device.bound 到设备 WS 失败(已忽略): {e}")

    return _add_cors(request, web.json_response(
        {"ok": True, "data": {"device_id": device_id, "status": "active"}}, dumps=_dumps
    ))


# ─────────────────────────────────────────────────────────
# 小程序：列出当前用户名下已绑定设备
# ─────────────────────────────────────────────────────────
async def handle_list(request):
    user = api_guard(request)
    if isinstance(user, web.Response):
        return _add_cors(request, user)
    uid = _user_id(user)
    binding = DeviceBindingStore()
    reg = get_connection_registry()
    usage = get_usage_store()
    # 今日用量按设备索引，避免每条设备各跑一次聚合
    daily = {s["device_id"]: s for s in usage.get_daily_summary()}
    # 近 7 天用量一次性批量聚合（新增 get_weekly_summary），消除原 N+1：
    # 原 handle_list 在循环内对每设备调用 get_device_summary(did, days=7)，
    # 每次都是一次无 start_ts 索引的全表扫描；对讲会话产生大量 device_usage
    # 记录后，设备列表查询退化为 N 次全表扫描 → 慢查询 + SQLite 锁竞争 →
    # 前端 catch 兜底成空列表。改为单条 SQL 批量聚合后，无论设备数/用量记录
    # 多少都是 1 次索引扫描。
    weekly_map = {s["device_id"]: s for s in usage.get_weekly_summary(7)}
    out = []
    for d in binding.list_all():
        if d.get("user_id") != uid:
            continue
        # 过滤已解绑（unbound）设备：解绑仅改 status 不删行，残留会混入列表
        if d.get("status") == "unbound":
            continue
        did = d.get("device_id", "")
        snap = reg.snapshot(did)
        last_active = usage.get_last_active(did)
        if snap:
            online = True
            connected_at = int(snap["connected_at"])
            client_ip = snap.get("client_ip")
            # 在线时以实时 last_active 为准
            last_active = int(max(last_active or 0, snap["connected_at"]))
        else:
            online = False
            connected_at = None
            client_ip = None
        today = daily.get(did) or {}
        weekly = weekly_map.get(did) or {}
        out.append({
            "device_id": did,
            "device_name": d.get("device_name"),
            "status": d.get("status"),
            "activated_at": d.get("activated_at"),
            "online": online,
            "connected_at": connected_at,
            "client_ip": client_ip,
            "last_active": last_active,
            "today": {
                "sessions": today.get("sessions", 0),
                "turns": today.get("turns", 0),
                "response_chars": today.get("response_chars", 0),
                "prompt_tokens": today.get("prompt_tokens", 0),
                "completion_tokens": today.get("completion_tokens", 0),
                "duration_seconds": today.get("duration_seconds", 0),
            },
            "weekly": {
                "sessions": weekly.get("sessions", 0),
                "turns": weekly.get("turns", 0),
                "response_chars": weekly.get("response_chars", 0),
                "prompt_tokens": weekly.get("prompt_tokens", 0),
                "completion_tokens": weekly.get("completion_tokens", 0),
                "duration_seconds": weekly.get("duration_seconds", 0),
                "last_active_ts": weekly.get("last_active_ts"),
            },
        })
    return _add_cors(request, web.json_response(
        {"ok": True, "data": out}, dumps=_dumps
    ))


# ─────────────────────────────────────────────────────────
# 小程序：设备用量明细（近 days 天汇总）
# ─────────────────────────────────────────────────────────
async def handle_device_usage(request):
    user = api_guard(request)
    if isinstance(user, web.Response):
        return _add_cors(request, user)
    uid = _user_id(user)
    device_id = (request.match_info.get("device_id") or "").strip()
    if not device_id:
        return _add_cors(request, _err(400, "缺少 device_id"))
    binding = DeviceBindingStore()
    # 大小写/形态归一化匹配（兼容 WS 上报大写 MAC 历史行），下游一律用精确存储 id
    owned = _get_owned_device(binding, uid, device_id)
    if not owned:
        return _add_cors(request, _err(404, "未找到该设备或无权限"))
    try:
        days = max(1, min(int(request.query.get("days", 7) or 7), 365))
    except Exception:
        days = 7
    summary = get_usage_store().get_device_summary(owned["device_id"], days=days) or {}
    return _add_cors(request, web.json_response(
        {"ok": True, "device_id": owned["device_id"], "days": days, "usage": summary},
        dumps=_dumps,
    ))


# ─────────────────────────────────────────────────────────
# 小程序：设备互动记忆「安全派生摘要」（不吐原始记忆）
# ?detail=true 时追加高光语录/记忆立方明细数组
# ─────────────────────────────────────────────────────────
async def handle_device_memory(request):
    user = api_guard(request)
    if isinstance(user, web.Response):
        return _add_cors(request, user)
    uid = _user_id(user)
    device_id = (request.match_info.get("device_id") or "").strip()
    if not device_id:
        return _add_cors(request, _err(400, "缺少 device_id"))
    binding = DeviceBindingStore()
    # 大小写/形态归一化匹配（兼容 WS 上报大写 MAC 历史行），下游一律用精确存储 id
    owned = _get_owned_device(binding, uid, device_id)
    if not owned:
        return _add_cors(request, _err(404, "未找到该设备或无权限"))
    detail = request.query.get("detail", "").lower() in ("1", "true", "yes")
    try:
        from fuguoai_core.providers.memory.mem_local_short.mem_local_short import read_role_memory
        raw, mtime = read_role_memory(owned["device_id"])
    except Exception as e:
        logger.error(f"读取设备记忆失败: {e}")
        raw, mtime = "", None
    return _add_cors(request, web.json_response(
        {"ok": True, "device_id": owned["device_id"], "memory": _summarize_memory(raw, mtime, detail=detail)},
        dumps=_dumps,
    ))


# ─────────────────────────────────────────────────────────
# 小程序：解绑设备
# ─────────────────────────────────────────────────────────
async def handle_unbind(request):
    user = api_guard(request)
    if isinstance(user, web.Response):
        return _add_cors(request, user)
    uid = _user_id(user)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    device_id = str((payload or {}).get("device_id") or "").strip()
    if not device_id:
        return _add_cors(request, _err(400, "缺少 device_id"))

    binding = DeviceBindingStore()
    # 大小写/形态归一化匹配（兼容 WS 上报大写 MAC 历史行），下游一律用精确存储 id
    owned = _get_owned_device(binding, uid, device_id)
    if not owned:
        return _add_cors(request, _err(404, "未找到该设备或无权限"))

    binding.set_status(owned["device_id"], "unbound", user_id=uid)
    binding.clear_device_token(owned["device_id"])
    logger.info(
        f"[审计] 设备解绑: user={uid} device={owned['device_id']} "
        f"time={time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() + 8 * 3600))} +08:00"
    )
    return _add_cors(request, web.json_response(
        {"ok": True, "device_id": owned["device_id"], "status": "unbound"}, dumps=_dumps
    ))


# ─────────────────────────────────────────────────────────
# 对讲（Intercom）会话控制（契约 v1.6.0 Part B，只扩不改）
# ─────────────────────────────────────────────────────────
async def handle_intercom(request):
    """POST /api/v1/devices/{device_id}/intercom

    Body: {"action": "start"|"stop", "peer": "<device_id>"|"loopback"}
    - start: IntercomRegistry.start() 判定 → established / peer_offline / peer_busy；
      established 时向两端下发 SetIntercom iot 指令（loopback 只发起端）。
    - stop: leave() 拆会话（对端收 peer_left），并向发起端下发 SetIntercom stop。
    - peer="loopback": 自环调试（M1，一台板子验 AEC），不查在线、不占忙线。
    """
    user = api_guard(request)
    if isinstance(user, web.Response):
        return _add_cors(request, user)
    uid = _user_id(user)
    device_id_raw = request.match_info.get("device_id", "").strip()
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    action = str((payload or {}).get("action") or "").strip()
    peer = str((payload or {}).get("peer") or "").strip()
    if action not in ("start", "stop"):
        return _add_cors(request, _err(400, "action 须为 start|stop"))
    if action == "start" and not peer:
        return _add_cors(request, _err(400, "start 需提供 peer"))

    binding = DeviceBindingStore()
    owned = _get_owned_device(binding, uid, device_id_raw)
    if not owned:
        return _add_cors(request, _err(404, "未找到该设备或无权限"))
    device_id = owned["device_id"]

    # peer 归属校验（loopback 例外；MVP 同账号设备互讲，别名映射归主 API 后续接）
    if action == "start" and peer != "loopback":
        owned_peer = _get_owned_device(binding, uid, peer)
        if not owned_peer:
            return _add_cors(request, _err(404, "对端设备未找到或无权限"))
        peer = owned_peer["device_id"]

    from fuguoai_core.utils.intercom_registry import get_intercom_registry
    intercom = get_intercom_registry()
    reg = get_connection_registry()

    def _set_intercom_cmd(_peer, _action):
        return {
            "type": "iot",
            "commands": [{
                "name": "DeviceControl",
                "method": "SetIntercom",
                "parameters": {"peer": _peer, "action": _action},
            }],
        }

    if action == "stop":
        await intercom.leave(device_id, "stop")
        ok_stop = await reg.send_to_device(device_id, _set_intercom_cmd(peer or "", "stop"))
        logger.info(f"[intercom][DEBUG] send SetIntercom stop -> device={device_id} ok={ok_stop}")
        logger.info(f"[intercom] stop: user={uid} device={device_id}")
        return _add_cors(request, web.json_response(
            {"ok": True, "data": {"device_id": device_id, "state": "stopped"}}, dumps=_dumps
        ))

    result = await intercom.start(device_id, peer)
    if result == "established":
        # 向两端下发 SetIntercom start（loopback 只发起端）
        ok_a = await reg.send_to_device(device_id, _set_intercom_cmd(peer, "start"))
        logger.info(f"[intercom][DEBUG] send SetIntercom start -> device={device_id} peer={peer} ok={ok_a}")
        if peer != "loopback":
            ok_b = await reg.send_to_device(peer, _set_intercom_cmd(device_id, "start"))
            logger.info(f"[intercom][DEBUG] send SetIntercom start -> peer={peer} ok={ok_b}")
    logger.info(f"[intercom] start: user={uid} device={device_id} peer={peer} -> {result}")
    return _add_cors(request, web.json_response(
        {"ok": result == "established",
         "data": {"device_id": device_id, "peer": peer, "state": result}},
        dumps=_dumps,
    ))


# ─────────────────────────────────────────────────────────
# 小程序日程管理（对接文档 §8，只扩不改）
# ─────────────────────────────────────────────────────────
_COMMAND_METHOD_ALLOW = {
    # §4 DeviceControl 全部方法透传；SetIntercom 走专用 /intercom 端点，禁在此重复入口
    "SetTimerTask", "CancelTimerTask", "ListTimerTasks",
    "SetMainLight", "SetDevice", "SetAmbientLight",
}


async def handle_device_command(request):
    """POST /api/v1/devices/{device_id}/command （对接文档 §8）

    Body: {"name":"DeviceControl","method":"SetTimerTask","parameters":{...}}
    行为：归属校验 → 包成 {"type":"iot","commands":[Body]} 经设备 WS 长连下行 →
    SetTimerTask 成功下发后保存后端副本(规避 §6.3 任务列表不进 states 快照)，
    CancelTimerTask 同步对账删除副本。设备不在线返回 ok:false + device_offline。
    """
    user = api_guard(request)
    if isinstance(user, web.Response):
        return _add_cors(request, user)
    uid = _user_id(user)
    device_id_raw = request.match_info.get("device_id", "").strip()
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    name = str((payload or {}).get("name") or "DeviceControl").strip()
    method = str((payload or {}).get("method") or "").strip()
    parameters = (payload or {}).get("parameters") or {}
    if not isinstance(parameters, dict):
        return _add_cors(request, _err(400, "parameters 须为对象"))
    if method not in _COMMAND_METHOD_ALLOW:
        return _add_cors(request, _err(
            400, f"method 不支持: {method or '(空)'}（对讲请用 /intercom 端点）"
        ))
    # SetTimerTask 参数预校验（与固件行为一致：越界固件直接丢弃且无 ack，这里提前拦）
    if method == "SetTimerTask":
        try:
            h, m = int(parameters.get("hour")), int(parameters.get("minute"))
        except (TypeError, ValueError):
            return _add_cors(request, _err(400, "SetTimerTask 需提供数字 hour/minute"))
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return _add_cors(request, _err(400, "hour 0-23 / minute 0-59"))

    binding = DeviceBindingStore()
    owned = _get_owned_device(binding, uid, device_id_raw)
    if not owned:
        return _add_cors(request, _err(404, "未找到该设备或无权限"))
    device_id = owned["device_id"]

    frame = {"type": "iot", "commands": [{
        "name": name, "method": method, "parameters": parameters,
    }]}
    reg = get_connection_registry()
    sent = await reg.send_to_device(device_id, frame)
    logger.info(
        f"[command] user={uid} device={device_id} method={method} sent={sent}"
    )
    if not sent:
        return _add_cors(request, web.json_response(
            {"ok": False, "error": "device_offline",
             "message": "设备不在线，指令未下发（设备仅经活跃 WS 接收 iot 指令）"},
            status=409, dumps=_dumps,
        ))

    data = {"device_id": device_id, "method": method, "sent": True}
    from fuguoai_core.utils.reminder_store import get_reminder_store
    if method == "SetTimerTask":
        rid = get_reminder_store().add(device_id, uid, parameters)
        if rid is not None:
            data["taskId"] = rid
    elif method == "CancelTimerTask":
        removed = get_reminder_store().cancel(
            device_id,
            rid=parameters.get("id"),
            hour=parameters.get("hour"),
            minute=parameters.get("minute"),
            repeat=parameters.get("repeat"),
        )
        data["removed"] = removed
    return _add_cors(request, web.json_response(
        {"ok": True, "data": data}, dumps=_dumps
    ))


async def handle_device_reminders(request):
    """GET /api/v1/devices/{device_id}/reminders （对接文档 §8 第4条 / 契约 §A.7.4）

    优先返回**设备上行镜像**（契约 A.7 真源，字段含 id/hour/minute/repeat/message/...）；
    设备从未上报过镜像时，回退到后端下发副本（reminder_store，字段含 rid/parameters）。
    source 字段标明数据来源，便于联调观测。
    """
    user = api_guard(request)
    if isinstance(user, web.Response):
        return _add_cors(request, user)
    uid = _user_id(user)
    device_id_raw = request.match_info.get("device_id", "").strip()
    binding = DeviceBindingStore()
    owned = _get_owned_device(binding, uid, device_id_raw)
    if not owned:
        return _add_cors(request, _err(404, "未找到该设备或无权限"))
    device_id = owned["device_id"]
    from fuguoai_core.utils.schedule_mirror_store import get_schedule_mirror_store
    from fuguoai_core.utils.reminder_store import get_reminder_store
    mirror = get_schedule_mirror_store().get(device_id)
    if mirror:
        items, source = mirror, "device_mirror"
    else:
        items, source = get_reminder_store().list(device_id), "backend_copy"
    return _add_cors(request, web.json_response(
        {"ok": True, "data": {"device_id": device_id,
                              "reminders": items, "count": len(items),
                              "source": source}},
        dumps=_dumps,
    ))


# ─────────────────────────────────────────────────────────
# 小程序：云端在线日程 CRUD（M2 在线日程，契约 §3.2 / §5.2，只扩不改）
#
# 云端任务是后端真源：小程序创建/修改/删除 → 落库 + 实时下行到设备
# （经 8000 WS schedule_push，不改 WS 协议格式）+ 广播 /api/v1/events 供小程序刷新。
# 设备离线时下行失败由调用方忽略——重连 schedule_pull 自然补齐。
# ─────────────────────────────────────────────────────────
async def handle_create_cloud_schedule(request):
    user = api_guard(request)
    if isinstance(user, web.Response):
        return _add_cors(request, user)
    uid = _user_id(user)
    device_id_raw = request.match_info.get("device_id", "").strip()
    binding = DeviceBindingStore()
    owned = _get_owned_device(binding, uid, device_id_raw)
    if not owned:
        return _add_cors(request, _err(404, "未找到该设备或无权限"))
    device_id = owned["device_id"]
    try:
        body = await request.json()
    except Exception:
        return _add_cors(request, _err(400, "请求体需为 JSON"))
    if not isinstance(body, dict):
        return _add_cors(request, _err(400, "请求体需为对象"))
    # 保护命名空间字段，交由存储层分配高位 id / source / modified_utc
    body.pop("id", None)
    body.pop("source", None)
    body.pop("modified_utc", None)
    from fuguoai_core.utils.schedule_mirror_store import get_schedule_mirror_store
    task = get_schedule_mirror_store().create_cloud_task(device_id, body)
    from fuguoai_core.utils.schedule_downlink import push_schedule_to_device, publish_cloud_change
    await push_schedule_to_device(device_id, tasks=[task], deletes=[])
    publish_cloud_change(device_id, "create", task=task)
    return _add_cors(request, web.json_response({"ok": True, "data": task}, dumps=_dumps))


async def handle_list_cloud_schedule(request):
    user = api_guard(request)
    if isinstance(user, web.Response):
        return _add_cors(request, user)
    uid = _user_id(user)
    device_id_raw = request.match_info.get("device_id", "").strip()
    binding = DeviceBindingStore()
    owned = _get_owned_device(binding, uid, device_id_raw)
    if not owned:
        return _add_cors(request, _err(404, "未找到该设备或无权限"))
    device_id = owned["device_id"]
    from fuguoai_core.utils.schedule_mirror_store import get_schedule_mirror_store
    items = get_schedule_mirror_store().get_cloud_tasks(device_id)
    return _add_cors(request, web.json_response(
        {"ok": True, "data": {"device_id": device_id, "schedules": items, "count": len(items)}},
        dumps=_dumps,
    ))


async def handle_update_cloud_schedule(request):
    user = api_guard(request)
    if isinstance(user, web.Response):
        return _add_cors(request, user)
    uid = _user_id(user)
    device_id_raw = request.match_info.get("device_id", "").strip()
    task_id_raw = request.match_info.get("task_id", "").strip()
    binding = DeviceBindingStore()
    owned = _get_owned_device(binding, uid, device_id_raw)
    if not owned:
        return _add_cors(request, _err(404, "未找到该设备或无权限"))
    device_id = owned["device_id"]
    try:
        task_id = int(task_id_raw)
    except ValueError:
        return _add_cors(request, _err(400, "task_id 非法"))
    try:
        body = await request.json()
    except Exception:
        return _add_cors(request, _err(400, "请求体需为 JSON"))
    if not isinstance(body, dict):
        return _add_cors(request, _err(400, "请求体需为对象"))
    body.pop("id", None)
    body.pop("source", None)
    body.pop("modified_utc", None)
    from fuguoai_core.utils.schedule_mirror_store import get_schedule_mirror_store
    updated = get_schedule_mirror_store().update_cloud_task(device_id, task_id, body)
    if updated is None:
        return _add_cors(request, _err(404, "云端日程不存在或不可更新"))
    from fuguoai_core.utils.schedule_downlink import push_schedule_to_device, publish_cloud_change
    await push_schedule_to_device(device_id, tasks=[updated], deletes=[])
    publish_cloud_change(device_id, "update", task=updated)
    return _add_cors(request, web.json_response({"ok": True, "data": updated}, dumps=_dumps))


async def handle_delete_cloud_schedule(request):
    user = api_guard(request)
    if isinstance(user, web.Response):
        return _add_cors(request, user)
    uid = _user_id(user)
    device_id_raw = request.match_info.get("device_id", "").strip()
    task_id_raw = request.match_info.get("task_id", "").strip()
    binding = DeviceBindingStore()
    owned = _get_owned_device(binding, uid, device_id_raw)
    if not owned:
        return _add_cors(request, _err(404, "未找到该设备或无权限"))
    device_id = owned["device_id"]
    try:
        task_id = int(task_id_raw)
    except ValueError:
        return _add_cors(request, _err(400, "task_id 非法"))
    from fuguoai_core.utils.schedule_mirror_store import get_schedule_mirror_store
    ok = get_schedule_mirror_store().delete_cloud_task(device_id, task_id)
    if not ok:
        return _add_cors(request, _err(404, "云端日程不存在或不可删除"))
    from fuguoai_core.utils.schedule_downlink import push_schedule_to_device, publish_cloud_change
    await push_schedule_to_device(device_id, tasks=[], deletes=[task_id])
    publish_cloud_change(device_id, "delete", deleted_id=task_id)
    return _add_cors(request, web.json_response(
        {"ok": True, "data": {"deleted_id": task_id}}, dumps=_dumps
    ))


async def handle_delete_device(request):
    """DELETE /api/v1/devices/{device_id}

    JWT 用户态：仅允许删除「自己名下」且「status=unbound」的设备（已绑定返回 403）。
    device_id 做大小写/形态归一化匹配（兼容 WS 上报大写 MAC 历史行），
    但 delete / 级联 / 审计一律用「精确存储 id」，避免误删或孤立历史数据。
    级联删除 device_binding 与 device_agent_config（usage / 对话历史保留审计）。
    """
    user = api_guard(request)
    if isinstance(user, web.Response):
        return _add_cors(request, user)
    uid = _user_id(user)
    # 原始大小写，不归一化（同 unbind 的 match_info 取法）
    device_id = (request.match_info.get("device_id") or "").strip()
    if not device_id:
        return _add_cors(request, _err(400, "缺少 device_id"))

    binding = DeviceBindingStore()
    # 大小写/形态归一化匹配（兼容 WS 上报大写 MAC 历史行），下游一律用精确存储 id
    did = normalize_device_id(device_id)
    owned = next(
        (d for d in binding.list_all() if normalize_device_id(d.get("device_id", "")) == did),
        None,
    )
    if not owned or owned.get("user_id") != uid:
        # 不向非所有者泄露设备存在性
        return _add_cors(request, _err(404, "未找到该设备或无权限"))
    if owned.get("status") != "unbound":
        return _add_cors(request, _err(403, "仅可删除已解绑(unbound)的设备"))

    binding.delete(owned["device_id"])
    logger.info(
        f"[审计] 设备删除: user={uid} device={owned['device_id']} "
        f"time={time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() + 8 * 3600))} +08:00"
    )
    try:
        from fuguoai_core.utils.agent_config_store import get_agent_config_store
        get_agent_config_store().delete_config(owned["device_id"])
    except Exception as e:
        # 角色配置缺失不影响主记录删除，记日志即可
        logging.getLogger("devices_routes").warning(
            f"级联删除 device_agent_config 失败 device={owned['device_id']}: {e}"
        )
    return _add_cors(request, web.json_response(
        {"ok": True, "device_id": owned["device_id"], "deleted": True}, dumps=_dumps
    ))
