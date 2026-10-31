"""M14 · 小程序实时事件推送端点（/api/v1/events）。

零侵入接入：由 fuguoai_core.api.registry 自动发现挂载，不改 http_server 既有路由。

红线合规（2026-07-12 22:01 用户硬约束）
--------------------------------------
- **独立通道**：本端点跑在 HTTP 服务口（默认 8003，nginx 反代 8002），是一条
  **全新的 WS 通道**，与设备固件用的 8000 语音 WS **完全隔离**，绝不复用、绝不改动
  设备 8000 协议与对话路径。
- 事件源自 core.utils.connection_registry 的旁路发布（event_bus），对话/断句/TTS
  路径零改动。
- 微信小程序不支持 SSE/EventSource，故用 WebSocket（wx.connectSocket 可带 header 鉴权）。

接口
----
GET /api/v1/events   [用户态守卫] 升级为 WebSocket；推送「当前用户名下设备」的实时事件。
  握手鉴权：Authorization: Bearer <小程序 JWT>（复用 core.guards.api_guard）。
  下行消息（JSON）：
    {"type":"hello", "online":[{device_id,connected_at,client_ip,last_active}], "ts":...}
    {"type":"device.online",  "device_id":..., "connected_at":..., "client_ip":..., "ts":...}
    {"type":"device.offline", "device_id":..., "ts":...}
    {"type":"device.active",  "device_id":..., "last_active":..., "ts":...}
    {"type":"ping", "ts":...}     # 服务端保活；客户端可回 {"type":"pong"}（可选）
  上行消息（可选）：{"type":"ping"} → 服务端回 {"type":"pong"}。
"""
import asyncio
import json
import logging
import threading
import time

from aiohttp import web

from fuguoai_core.api.registry import register_routes
from fuguoai_core.guards import api_guard
from fuguoai_core.device_access import DeviceBindingStore
from fuguoai_core.utils.connection_registry import get_connection_registry
from fuguoai_core.utils.event_bus import get_event_bus

TAG = "events_routes"
logger = logging.getLogger(TAG)

# 服务端保活间隔（秒）：queue 空闲超过此值就发一次 ping。
_PING_INTERVAL = 25.0
# owned 设备集合刷新节流（秒）：收到未知设备事件时最多每这么久重查一次归属。
_OWNED_REFRESH_INTERVAL = 5.0
# 单用户并发 WebSocket 上限：防止单个用户开大量连接耗尽事件推送资源。
_MAX_CONNS_PER_USER = 5
# {uid: 当前并发连接数}，配合 _CONN_LOCK 维护；自减在 finally 中回收配额。
_conn_counts: dict = {}
_CONN_LOCK = threading.Lock()


def _user_id(user: dict) -> str:
    return (user or {}).get("user_id") or (user or {}).get("openid")


def _owned_device_ids(uid: str) -> set:
    try:
        return {
            d["device_id"]
            for d in DeviceBindingStore().list_all()
            if d.get("user_id") == uid and d.get("device_id")
        }
    except Exception as e:
        logger.error(f"查询用户设备归属失败 uid={uid}: {e}")
        return set()


@register_routes
def register(app):
    app.router.add_get("/api/v1/events", handle_events)


async def handle_events(request):
    # 握手鉴权：WS upgrade 前先跑用户态守卫，失败直接返回普通 401 响应（不升级）。
    user = api_guard(request)
    if isinstance(user, web.Response):
        return user
    uid = _user_id(user)
    if not uid:
        return web.json_response({"code": 400, "message": "无法确定当前用户"}, status=400)

    # 单用户并发连接数限制：拒绝超额连接，避免单个用户耗尽事件推送资源。
    with _CONN_LOCK:
        cur = _conn_counts.get(uid, 0)
        if cur >= _MAX_CONNS_PER_USER:
            return web.json_response(
                {"code": 429, "message": f"实时连接数已达上限({_MAX_CONNS_PER_USER})"},
                status=429,
            )
        _conn_counts[uid] = cur + 1

    try:
        ws = web.WebSocketResponse(heartbeat=None)
        if not ws.can_prepare(request).ok:
            return web.json_response({"code": 400, "message": "需要 WebSocket 升级"}, status=400)
        await ws.prepare(request)

        bus = get_event_bus()
        reg = get_connection_registry()
        sid, queue = bus.subscribe()
        owned = _owned_device_ids(uid)
        last_owned_refresh = time.time()

        # 建连即下发 hello：当前用户名下「正在线」的设备快照，供面板首屏渲染。
        try:
            online = []
            for did in owned:
                snap = reg.snapshot(did)
                if snap:
                    online.append({
                        "device_id": did,
                        "connected_at": int(snap["connected_at"]),
                        "client_ip": snap.get("client_ip"),
                        "last_active": int(snap.get("last_active") or snap["connected_at"]),
                    })
            await ws.send_json({"type": "hello", "online": online, "ts": int(time.time())})
        except Exception as e:
            logger.error(f"事件 WS 处理异常: {e}")

        async def _recv():
            """读取客户端上行：处理 ping/close。收到 close 或异常即结束。"""
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data) if msg.data else {}
                    except Exception:
                        data = {}
                    if isinstance(data, dict) and data.get("type") == "ping":
                        try:
                            await ws.send_json({"type": "pong", "ts": int(time.time())})
                        except Exception:
                            break
                elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSING,
                                  web.WSMsgType.CLOSED, web.WSMsgType.ERROR):
                    break

        async def _push():
            """从事件总线取事件，按用户设备归属过滤后下推；空闲则发 ping 保活。"""
            nonlocal owned, last_owned_refresh
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_PING_INTERVAL)
                except asyncio.TimeoutError:
                    try:
                        await ws.send_json({"type": "ping", "ts": int(time.time())})
                        continue
                    except Exception:
                        break
                did = (event or {}).get("device_id")
                if not did:
                    continue
                # 未知设备：可能是握手后新绑定的，节流刷新一次归属集合
                if did not in owned:
                    now = time.time()
                    if now - last_owned_refresh >= _OWNED_REFRESH_INTERVAL:
                        owned = _owned_device_ids(uid)
                        last_owned_refresh = now
                    if did not in owned:
                        continue
                try:
                    await ws.send_json(event)
                except Exception:
                    break

        recv_task = asyncio.ensure_future(_recv())
        push_task = asyncio.ensure_future(_push())
        try:
            done, pending = await asyncio.wait(
                {recv_task, push_task}, return_when=asyncio.FIRST_COMPLETED
            )
            # 取消仍挂起的对端任务后必须 await 它，否则触发
            # “task was destroyed but it is pending” 警告并可能泄漏协程。
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for t in done:
                if not t.cancelled() and t.exception() is not None:
                    logger.warning(f"事件 WS 子任务异常: {t.exception()}")
        finally:
            bus.unsubscribe(sid)
            if not ws.closed:
                try:
                    await ws.close()
                except Exception as e:
                    logger.error(f"事件 WS 关闭异常: {e}")
        return ws
    finally:
        # 归还本连接的并发配额。
        with _CONN_LOCK:
            n = _conn_counts.get(uid, 0)
            if n > 0:
                _conn_counts[uid] = n - 1
