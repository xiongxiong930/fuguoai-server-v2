import json
import time
import asyncio
import os
import base64
from aiohttp import web
from fuguoai_config.logger import setup_logging
logger = setup_logging()
from fuguoai_core.api.ota_handler import OTAHandler
from fuguoai_core.api.vision_handler import VisionHandler
from fuguoai_core.api.registry import apply_routes
from fuguoai_core.utils.metrics import METRICS
from fuguoai_core.utils.circuit_breaker import all_breakers
from fuguoai_core.utils.config_holder import (
    get_config,
    reload as reload_config,
    get_last_reload,
)
from fuguoai_core.device_access import DeviceBindingStore
from fuguoai_core.utils.usage_store import get_usage_store
from fuguoai_core.utils.voiceprint_provider import get_voiceprint_provider

_binding_store = DeviceBindingStore()  # 模块级单例，与 connection 共享同一 db

TAG = __name__

# ====================== V5 · /admin 访问令牌（可选） ======================
# 设置环境变量 FUGUOAI_ADMIN_TOKEN 后，/admin 系列端点需携带
# `Authorization: Bearer <token>` 或 `Basic any:<token>` 才能访问。
# 未设置时保持完全开放（不影响固件联调）。
ADMIN_TOKEN = (os.environ.get("FUGUOAI_ADMIN_TOKEN") or "").strip()


def _admin_guard(request):
    """运营面板鉴权守卫：未配置令牌则拒绝（fail-closed）；配置后要求 Bearer/Basic。

    返回 None 表示放行；返回 web.Response(401) 表示拒绝。
    """
    if not ADMIN_TOKEN:
        logger.bind(tag=TAG).critical(
            "运营面板未配置 FUGUOAI_ADMIN_TOKEN，拒绝所有 /admin 访问（fail-closed）"
        )
        return web.json_response(
            {"error": "unauthorized", "message": "运营面板需要访问令牌"},
            status=401,
            headers={"WWW-Authenticate": 'Bearer realm="fuguoai-admin"'},
        )
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        if auth[len("Bearer "):].strip() == ADMIN_TOKEN:
            return None
    elif auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode("utf-8", "replace")
            pwd = decoded.split(":", 1)[1] if ":" in decoded else ""
            if pwd == ADMIN_TOKEN:
                return None
        except Exception as e:
            logger.bind(tag=TAG).error(f"运营面板认证解析异常: {e}")
    return web.json_response(
        {"error": "unauthorized", "message": "运营面板需要访问令牌"},
        status=401,
        headers={"WWW-Authenticate": 'Bearer realm="fuguoai-admin"'},
    )


async def _run_sync(fn, *args):
    """把同步阻塞调用（sqlite / 文件 I/O）丢线程池，避免阻塞 WS 共享事件循环。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fn, *args)


class SimpleHttpServer:
    def __init__(self, config: dict):
        self.config = config
        self.logger = setup_logging()
        self.ota_handler = OTAHandler(config)
        self.vision_handler = VisionHandler(config)

    async def handle_metrics(self, request):
        """Prometheus 文本格式指标端点，供监控系统抓取。"""
        return web.Response(
            text=METRICS.render(),
            content_type="text/plain",
            charset="utf-8",
        )

    async def handle_healthz(self, request):
        """健康检查端点：汇总熔断器状态与活跃连接数。

        status=ok（全部闭合/半开）| degraded（存在开路熔断器）。
        """
        circuits = [cb.snapshot() for cb in all_breakers()]
        any_open = any(c["state"] == "open" for c in circuits)
        gauges = METRICS.snapshot_gauges()
        active = 0.0
        for key, val in gauges.get("fuguoai_ws_connections_active", {}).items():
            active += val
        body = {
            "status": "degraded" if any_open else "ok",
            "timestamp": int(time.time()),
            "active_connections": int(active),
            "circuits": circuits,
        }
        return web.json_response(
            body, status=200, dumps=lambda o: json.dumps(o, ensure_ascii=False)
        )

    # ====================== V5 · /admin 运营面板 ======================
    async def handle_admin(self, request):
        """运营面板主页（静态 HTML，前端 fetch /admin/summary 渲染）。"""
        g = _admin_guard(request)
        if g is not None:
            return g
        return web.Response(
            text=_ADMIN_PANEL_HTML,
            content_type="text/html",
            charset="utf-8",
        )

    async def handle_admin_summary(self, request):
        """聚合运营指标，供面板渲染。"""
        g = _admin_guard(request)
        if g is not None:
            return g
        counters = METRICS.snapshot_counters()
        gauges = METRICS.snapshot_gauges()
        circuits = [cb.snapshot() for cb in all_breakers()]

        def _sum_counter(name):
            vals = counters.get(name, {})
            return sum(float(v) for v in vals.values())

        active = 0.0
        for v in gauges.get("fuguoai_ws_connections_active", {}).values():
            active += float(v)

        rag_q = _sum_counter("fuguoai_memory_rag_queries_total")
        rag_h = _sum_counter("fuguoai_memory_rag_hits_total")
        hit_rate = (rag_h / rag_q) if rag_q > 0 else None

        # M9：从 METRICS label 中提取设备级统计
        def _device_breakdown(counter_name):
            """{device_id: sum_value}"""
            result = {}
            per_label = counters.get(counter_name, {})
            for labels_tuple, val in per_label.items():
                for k, v in labels_tuple:
                    if k == "device_id":
                        result[v] = result.get(v, 0) + int(float(val))
            return result

        device_sessions = _device_breakdown("fuguoai_device_sessions_total")
        device_turns = _device_breakdown("fuguoai_conversation_turns_total")
        all_device_ids = set(device_sessions.keys()) | set(device_turns.keys())
        devices = {}
        for did in sorted(all_device_ids):
            devices[did] = {
                "sessions": device_sessions.get(did, 0),
                "turns": device_turns.get(did, 0),
            }

        body = {
            "ok": True,
            "uptime_seconds": int(METRICS.uptime_seconds()),
            "active_connections": int(active),
            "counters": {
                "llm_requests": int(_sum_counter("fuguoai_llm_requests_total")),
                "tts_requests": int(_sum_counter("fuguoai_tts_requests_total")),
                "rag_queries": int(rag_q),
                "rag_hits": int(rag_h),
                "device_sessions": int(_sum_counter("fuguoai_device_sessions_total")),
                "conversation_turns": int(_sum_counter("fuguoai_conversation_turns_total")),
            },
            "devices": devices,
            "memory_hit_rate": round(hit_rate, 4) if hit_rate is not None else None,
            "circuits": circuits,
            "config_keys": sorted(get_config().keys()),
            "last_reload": get_last_reload(),
            "admin_auth": bool(ADMIN_TOKEN),
        }
        return web.json_response(
            body, status=200, dumps=lambda o: json.dumps(o, ensure_ascii=False)
        )

    async def handle_admin_config(self, request):
        """返回当前生效配置（脱敏，隐藏密钥类字段）。"""
        g = _admin_guard(request)
        if g is not None:
            return g
        redacted = _redact(get_config())
        return web.json_response(
            redacted, status=200, dumps=lambda o: json.dumps(o, ensure_ascii=False)
        )

    async def handle_admin_devices(self, request):
        """设备列表：合并绑定表与用量表，含今日用量、最近活跃、实时在线态。"""
        g = _admin_guard(request)
        if g is not None:
            return g
        try:
            from fuguoai_core.utils.connection_registry import get_connection_registry
            reg = get_connection_registry()
        except Exception:
            reg = None
        usage = get_usage_store()
        binding_list = await _run_sync(_binding_store.list_all)
        binding_map = {b["device_id"]: b for b in binding_list}
        devices = []
        seen = set()
        for did, b in binding_map.items():
            seen.add(did)
            s = usage.get_device_summary(did, days=1) or {}
            online = reg.is_online(did) if reg else bool(b.get("online"))
            devices.append(self._device_card(
                did, b.get("status") or "guest", b.get("user_id"), b.get("device_name"),
                s, online=online, last_seen=b.get("last_seen")))
        for did in usage.get_all_device_ids():
            if did in seen:
                continue
            seen.add(did)
            s = usage.get_device_summary(did, days=1) or {}
            devices.append(self._device_card(did, "guest", None, None, s, online=False, last_seen=None))
        return web.json_response(
            {"ok": True, "devices": devices},
            dumps=lambda o: json.dumps(o, ensure_ascii=False),
        )

    @staticmethod
    def _device_card(did, status, user_id, name, s, online=False, last_seen=None):
        return {
            "device_id": did,
            "status": status,
            "user_id": user_id,
            "device_name": name,
            "online": online,
            "last_seen": last_seen,
            "today_sessions": s.get("sessions", 0),
            "today_turns": s.get("turns", 0),
            "today_duration_seconds": s.get("duration_seconds", 0),
            "last_active_ts": s.get("last_active_ts"),
        }

    async def handle_admin_device_detail(self, request):
        """设备详情：绑定信息 + 近 N 天用量汇总。"""
        g = _admin_guard(request)
        if g is not None:
            return g
        did = request.match_info.get("device_id")
        binding_list = await _run_sync(_binding_store.list_all)
        binding = next((b for b in binding_list if b["device_id"] == did), None)
        usage = get_usage_store()
        try:
            days = int(request.query.get("days", 7))
        except Exception:
            days = 7
        summary = usage.get_device_summary(did, days=days) or {}
        return web.json_response(
            {"ok": True, "device_id": did, "binding": binding,
             "summary": {"days": days, **summary}},
            dumps=lambda o: json.dumps(o, ensure_ascii=False),
        )

    async def handle_admin_device_bind(self, request):
        """绑定设备（供小程序管理端调用）：写库并标 active。"""
        g = _admin_guard(request)
        if g is not None:
            return g
        did = request.match_info.get("device_id")
        try:
            body = await request.json()
        except Exception:
            body = {}
        user_id = body.get("user_id")
        device_name = body.get("device_name")
        _binding_store.set_status(did, "active", user_id)
        if device_name:
            _binding_store.set_device_name(did, device_name)
        return web.json_response(
            {"ok": True, "device_id": did, "status": "active"},
            dumps=lambda o: json.dumps(o, ensure_ascii=False),
        )

    async def handle_admin_device_unbind(self, request):
        """解绑设备：状态置 unbound，并清除 device_token（与小程序解绑语义一致），
        使设备无法再用旧 token 重连 WS。不改 8000 协议、不重命名字段。"""
        g = _admin_guard(request)
        if g is not None:
            return g
        did = request.match_info.get("device_id")
        if not did:
            return web.json_response(
                {"ok": False, "error": "missing device_id"}, status=400,
                dumps=lambda o: json.dumps(o, ensure_ascii=False),
            )
        try:
            from fuguoai_core.utils.util import normalize_device_id
            did = normalize_device_id(did)
        except Exception:
            pass
        _binding_store.set_status(did, "unbound")
        try:
            _binding_store.clear_device_token(did)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"解绑清除 token 异常: {e}")
        return web.json_response(
            {"ok": True, "device_id": did, "status": "unbound", "token_cleared": True},
            dumps=lambda o: json.dumps(o, ensure_ascii=False),
        )

    async def handle_admin_device_kick(self, request):
        """踢设备：主动断开该设备当前活跃 WS 连接，并立即落库 offline。

        供解绑 / 强制下线场景调用（小程序解绑走 manage 服务 → 调此端点）。
        鉴权复用 _admin_guard（FUGUOAI_ADMIN_TOKEN）。device_id 入参按归一化规则
        统一为小写，以匹配连接注册表键。
        """
        g = _admin_guard(request)
        if g is not None:
            return g
        did = request.match_info.get("device_id")
        if not did:
            return web.json_response(
                {"ok": False, "error": "missing device_id"}, status=400,
                dumps=lambda o: json.dumps(o, ensure_ascii=False),
            )
        # 归一化，匹配连接注册表键（固件 SN 可能大小写不一）
        try:
            from fuguoai_core.utils.util import normalize_device_id
            did = normalize_device_id(did)
        except Exception:
            pass
        closed = 0
        try:
            from fuguoai_core.utils.connection_registry import get_connection_registry
            for factory in get_connection_registry().kick(did):
                try:
                    # 后台触发关闭，立即返回响应；连接自身的 finally->_save_and_close
                    # 会完成清理+落库 offline+计数回落，避免跨协程 await 自身任务挂起。
                    asyncio.create_task(factory())
                    closed += 1
                except Exception as e:
                    self.logger.bind(tag=TAG).error(f"kick 触发关闭异常: {e}")
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"kick 处理异常: {e}")
        # 立即落库 offline（带超时，失败不影响响应），避免等 30s 离线判定定时器
        try:
            from fuguoai_core.native.device_store import set_online_async
            await asyncio.wait_for(set_online_async(did, 0), timeout=3)
        except Exception:
            pass
        # 踢人同时吊销设备令牌：未来开启 WS 鉴权(device_ws_auth)后，被踢设备凭旧
        # token 将无法重连语音 WS；与 unbind 语义对齐（token 即设备凭证）。
        try:
            _binding_store.clear_device_token(did)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"kick 清除 token 异常: {e}")
        return web.json_response(
            {"ok": True, "device_id": did, "closed": closed},
            dumps=lambda o: json.dumps(o, ensure_ascii=False),
        )

    async def handle_admin_usage(self, request):
        """用量查询：支持按 device_id 过滤，days 控制时间窗。"""
        g = _admin_guard(request)
        if g is not None:
            return g
        did = request.query.get("device_id")
        try:
            days = int(request.query.get("days", 7))
        except Exception:
            days = 7
        usage = get_usage_store()
        if did:
            items = [{"device_id": did, **(usage.get_device_summary(did, days=days) or {})}]
        else:
            items = [
                {"device_id": d, **(usage.get_device_summary(d, days=days) or {})}
                for d in usage.get_all_device_ids()
            ]
        return web.json_response(
            {"ok": True, "days": days, "devices": items},
            dumps=lambda o: json.dumps(o, ensure_ascii=False),
        )

    # ====================== V5 · /admin/voiceprint 说话人识别 ======================
    async def handle_admin_voiceprint_list(self, request):
        """列出已登记说话人（及启用状态 / group_id）。"""
        g = _admin_guard(request)
        if g is not None:
            return g
        provider = get_voiceprint_provider(self.config)
        if provider is None:
            return web.json_response(
                {
                    "enabled": False,
                    "error": "声纹未配置（需 voiceprint.provider=tencent + 密钥 + group_id）",
                },
                dumps=lambda o: json.dumps(o, ensure_ascii=False),
            )
        return web.json_response(
            provider.list_speakers(),
            dumps=lambda o: json.dumps(o, ensure_ascii=False),
        )

    async def handle_admin_voiceprint_enroll(self, request):
        """登记一名说话人：body {name, wav_base64}（wav_base64 = WAV 字节 base64）。"""
        g = _admin_guard(request)
        if g is not None:
            return g
        provider = get_voiceprint_provider(self.config)
        if provider is None:
            return web.json_response(
                {"ok": False, "error": "声纹未配置"},
                status=400,
                dumps=lambda o: json.dumps(o, ensure_ascii=False),
            )
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "JSON 解析失败"},
                status=400,
                dumps=lambda o: json.dumps(o, ensure_ascii=False),
            )
        name = (body.get("name") or "").strip()
        wav_b64 = body.get("wav_base64") or body.get("audio")
        if not name or not wav_b64:
            return web.json_response(
                {"ok": False, "error": "需要 name 与 wav_base64"},
                status=400,
                dumps=lambda o: json.dumps(o, ensure_ascii=False),
            )
        try:
            wav = base64.b64decode(wav_b64)
        except Exception:
            return web.json_response(
                {"ok": False, "error": "wav_base64 解码失败"},
                status=400,
                dumps=lambda o: json.dumps(o, ensure_ascii=False),
            )
        result = await provider.enroll_speaker(name, wav)
        status = 200 if result.get("ok") else 400
        return web.json_response(
            result, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False)
        )

    async def handle_admin_voiceprint_delete(self, request):
        """删除一名说话人（按 voiceprint_id）。"""
        g = _admin_guard(request)
        if g is not None:
            return g
        provider = get_voiceprint_provider(self.config)
        if provider is None:
            return web.json_response(
                {"ok": False, "error": "声纹未配置"},
                status=400,
                dumps=lambda o: json.dumps(o, ensure_ascii=False),
            )
        vid = request.match_info.get("voiceprint_id", "")
        result = await provider.delete_speaker(vid)
        return web.json_response(
            result, dumps=lambda o: json.dumps(o, ensure_ascii=False)
        )

    async def handle_admin_reload(self, request):
        """触发配置热加载（零重启）。支持 POST 与 OPTIONS。"""
        if request.method == "OPTIONS":
            return web.Response(
                text="",
                headers={
                    "Access-Control-Allow-Methods": "POST, OPTIONS",
                    "Access-Control-Allow-Headers": "content-type, authorization",
                    "Access-Control-Allow-Origin": "*",
                },
            )
        g = _admin_guard(request)
        if g is not None:
            return g
        result = await reload_config(trigger="manual")
        status = 200 if result.get("ok") else 500
        return web.json_response(
            result, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False)
        )

    def _get_websocket_url(self, local_ip: str, port: int) -> str:
        """获取websocket地址

        Args:
            local_ip: 本地IP地址
            port: 端口号

        Returns:
            str: websocket地址
        """
        server_config = self.config["server"]
        websocket_config = server_config.get("websocket")

        if websocket_config and "你" not in websocket_config:
            return websocket_config
        else:
            return f"ws://{local_ip}:{port}/fuguoai/v1/"

    async def start(self):
        try:
            server_config = self.config["server"]
            read_config_from_api = self.config.get("read_config_from_api", False)
            host = server_config.get("ip", "0.0.0.0")
            port = int(server_config.get("http_port", 8003))

            if port:
                app = web.Application()

                if not read_config_from_api:
                    # 如果没有开启智控台，只是单模块运行，就需要再添加简单OTA接口，用于下发websocket接口
                    app.add_routes(
                        [
                            web.get("/fuguoai/ota/", self.ota_handler.handle_get),
                            web.post("/fuguoai/ota/", self.ota_handler.handle_post),
                            web.options(
                                "/fuguoai/ota/", self.ota_handler.handle_options
                            ),
                            # 下载接口，仅提供 data/bin/*.bin 下载
                            web.get(
                                "/fuguoai/ota/download/{filename}",
                                self.ota_handler.handle_download,
                            ),
                            web.options(
                                "/fuguoai/ota/download/{filename}",
                                self.ota_handler.handle_options,
                            ),
                        ]
                    )
                # 添加路由
                app.add_routes(
                    [
                        web.get("/mcp/vision/explain", self.vision_handler.handle_get),
                        web.post(
                            "/mcp/vision/explain", self.vision_handler.handle_post
                        ),
                        web.options(
                            "/mcp/vision/explain", self.vision_handler.handle_options
                        ),
                        # V3 可观测性：Prometheus 指标 + 健康检查
                        web.get("/metrics", self.handle_metrics),
                        web.get("/healthz", self.handle_healthz),
                        # V5 运营面板与配置热加载
                        web.get("/admin", self.handle_admin),
                        web.get("/admin/summary", self.handle_admin_summary),
                        web.get("/admin/config", self.handle_admin_config),
                        web.post("/admin/reload", self.handle_admin_reload),
                        web.options("/admin/reload", self.handle_admin_reload),
                        web.get("/admin/devices", self.handle_admin_devices),
                        web.get("/admin/devices/{device_id}", self.handle_admin_device_detail),
                        web.post("/admin/devices/{device_id}/bind", self.handle_admin_device_bind),
                        web.post("/admin/devices/{device_id}/unbind", self.handle_admin_device_unbind),
                        web.post("/admin/devices/{device_id}/kick", self.handle_admin_device_kick),
                        web.get("/admin/usage", self.handle_admin_usage),
                        # 说话人识别（腾讯云）运营接口
                        web.get("/admin/voiceprint", self.handle_admin_voiceprint_list),
                        web.post("/admin/voiceprint/enroll", self.handle_admin_voiceprint_enroll),
                        web.post(
                            "/admin/voiceprint/{voiceprint_id}/delete",
                            self.handle_admin_voiceprint_delete,
                        ),
                    ]
                )

                # 扩展点：挂载所有通过 fuguoai_core.api.registry 自登记的功能路由
                # （设备配置 M12、知识库、社交竞技……），不改动上方既有路由。
                apply_routes(app)

                # 运行服务
                runner = web.AppRunner(app)
                await runner.setup()
                site = web.TCPSite(runner, host, port)
                await site.start()

                # 保持服务运行
                while True:
                    await asyncio.sleep(3600)  # 每隔 1 小时检查一次
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"HTTP服务器启动失败: {e}")
            import traceback

            self.logger.bind(tag=TAG).error(f"错误堆栈: {traceback.format_exc()}")
            raise


# ---------------- 工具函数 ----------------
def _redact(obj, _depth=0):
    """递归脱敏：隐藏含 key/token/secret/password 的字段值。"""
    if _depth > 12:
        return obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if any(s in kl for s in ("key", "token", "secret", "password", "auth")):
                if v and not str(v).startswith("你"):
                    out[k] = "***REDACTED***"
                else:
                    out[k] = v
            else:
                out[k] = _redact(v, _depth + 1)
        return out
    if isinstance(obj, list):
        return [_redact(x, _depth + 1) for x in obj]
    return obj


# ---------------- /admin 静态面板（深色主题，前端 fetch 渲染） ----------------
_ADMIN_PANEL_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>复果AI · 运营面板</title>
<style>
  :root{
    --bg:#0f1420; --panel:#171e2e; --panel2:#1f283b; --line:#2a3550;
    --txt:#e6ecf5; --muted:#8a96ad; --accent:#4f8cff; --green:#3ddc84;
    --yellow:#ffcf5c; --red:#ff6b6b; --chip:#243049;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;}
  header{padding:22px 28px;border-bottom:1px solid var(--line);display:flex;
    align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px}
  header h1{font-size:19px;margin:0;font-weight:650;letter-spacing:.3px}
  header .sub{color:var(--muted);font-size:13px;margin-top:4px}
  .wrap{padding:24px 28px;max-width:1180px;margin:0 auto}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:16px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px 18px 16px}
  .card .label{color:var(--muted);font-size:12.5px;margin-bottom:10px}
  .card .value{font-size:27px;font-weight:680;line-height:1}
  .card .value small{font-size:13px;color:var(--muted);font-weight:500;margin-left:4px}
  .bar{height:6px;border-radius:6px;background:var(--panel2);margin-top:14px;overflow:hidden}
  .bar > i{display:block;height:100%;background:var(--accent)}
  section{margin-top:26px}
  section h2{font-size:14px;color:var(--muted);font-weight:600;text-transform:uppercase;
    letter-spacing:1px;margin:0 0 12px}
  .cb{display:flex;flex-wrap:wrap;gap:10px}
  .breaker{background:var(--panel);border:1px solid var(--line);border-radius:10px;
    padding:10px 14px;font-size:13px;display:flex;align-items:center;gap:9px}
  .dot{width:9px;height:9px;border-radius:50%}
  .dot.closed{background:var(--green)} .dot.half-open{background:var(--yellow)}
  .dot.open{background:var(--red)}
  .keys{display:flex;flex-wrap:wrap;gap:8px}
  .key{background:var(--chip);border:1px solid var(--line);border-radius:8px;
    padding:5px 11px;font-size:12.5px;color:var(--muted)}
  .toolbar{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
  button{background:var(--accent);color:#fff;border:0;border-radius:10px;
    padding:10px 18px;font-size:14px;font-weight:600;cursor:pointer}
  button:disabled{opacity:.55;cursor:default}
  .status{font-size:13px;color:var(--muted)}
  .status b{color:var(--txt)}
  .foot{margin-top:30px;color:var(--muted);font-size:12px;text-align:center}
  code{background:var(--panel2);padding:2px 7px;border-radius:6px;font-size:12px}
</style>
</head>
<body>
<header>
  <div>
    <h1>复果AI · 语音网关运营面板</h1>
    <div class="sub">V5 · 零重启运维 &amp; 运营视图 · 数据每 5s 自动刷新</div>
  </div>
  <div class="toolbar">
    <span class="status" id="reloadStatus">最近重载：—</span>
    <button id="reloadBtn" onclick="doReload()">重新加载配置</button>
  </div>
</header>
<div class="wrap">
  <div class="grid">
    <div class="card"><div class="label">运行时长</div>
      <div class="value" id="uptime">—</div></div>
    <div class="card"><div class="label">活跃连接</div>
      <div class="value" id="active">—</div></div>
    <div class="card"><div class="label">LLM 请求</div>
      <div class="value" id="llm">—</div></div>
    <div class="card"><div class="label">TTS 请求</div>
      <div class="value" id="tts">—</div></div>
    <div class="card"><div class="label">记忆查询</div>
      <div class="value" id="ragq">—</div></div>
    <div class="card"><div class="label">记忆命中率</div>
      <div class="value" id="ragh">—</div>
      <div class="bar"><i id="raghBar" style="width:0%"></i></div></div>
  </div>

  <section><h2>熔断器状态</h2>
    <div class="cb" id="circuits"><span class="status">加载中…</span></div>
  </section>

  <section><h2>当前生效配置键（<code>/admin/config</code> 已脱敏）</h2>
    <div class="keys" id="cfgkeys"><span class="status">加载中…</span></div>
  </section>
</div>
<div class="foot">复果AI 后端 · fuguoai-server · 指标取自 <code>/metrics</code> · 配置热加载 <code>POST /admin/reload</code></div>

<script>
function fmt(n){ if(n===null||n===undefined) return '—'; return n.toLocaleString(); }
function fmtUptime(s){
  if(!s && s!==0) return '—';
  const h=Math.floor(s/3600), m=Math.floor((s%3600)/60);
  return h+'h '+m+'m';
}
let _adminToken = sessionStorage.getItem('fuguoai_admin_token') || '';
async function api(path, opts){
  opts = opts || {};
  opts.headers = opts.headers || {};
  if(_adminToken) opts.headers['Authorization'] = 'Bearer ' + _adminToken;
  let r = await fetch(path, opts);
  if(r.status === 401){
    const t = prompt('运营面板需要访问令牌：');
    if(t){ _adminToken = t; sessionStorage.setItem('fuguoai_admin_token', t); return api(path, opts); }
  }
  return r;
}
async function load(){
  try{
    const r = await api('/admin/summary');
    const d = await r.json();
    document.getElementById('uptime').textContent = fmtUptime(d.uptime_seconds);
    document.getElementById('active').textContent = fmt(d.active_connections);
    document.getElementById('llm').textContent = fmt(d.counters.llm_requests);
    document.getElementById('tts').textContent = fmt(d.counters.tts_requests);
    document.getElementById('ragq').textContent = fmt(d.counters.rag_queries);
    const hr = d.memory_hit_rate;
    document.getElementById('ragh').textContent = hr===null?'—':(hr*100).toFixed(1)+'%';
    document.getElementById('raghBar').style.width = hr===null?'0%':(hr*100).toFixed(1)+'%';
    // circuits
    const c = document.getElementById('circuits'); c.innerHTML='';
    (d.circuits||[]).forEach(b=>{
      const el=document.createElement('div'); el.className='breaker';
      el.innerHTML='<span class="dot '+b.state+'"></span>'+b.name+
        ' · <b>'+b.state+'</b> · 失败 '+b.failure_count;
      c.appendChild(el);
    });
    if(!(d.circuits||[]).length) c.innerHTML='<span class="status">无熔断器</span>';
    // config keys
    const k=document.getElementById('cfgkeys'); k.innerHTML='';
    (d.config_keys||[]).forEach(key=>{
      const s=document.createElement('span'); s.className='key'; s.textContent=key; k.appendChild(s);
    });
    // last reload
    const lr=d.last_reload||{};
    document.getElementById('reloadStatus').innerHTML = lr.ts
      ? '最近重载：<b>'+(lr.ok?'成功':'失败')+'</b> · '+lr.elapsed_ms+'ms · '+
        new Date(lr.ts*1000).toLocaleString()
      : '最近重载：—';
  }catch(e){
    document.getElementById('reloadStatus').textContent='加载失败：'+e;
  }
}
async function doReload(){
  const btn=document.getElementById('reloadBtn');
  btn.disabled=true; btn.textContent='加载中…';
  try{
    const r=await api('/admin/reload',{method:'POST'});
    const d=await r.json();
    document.getElementById('reloadStatus').innerHTML =
      '最近重载：<b>'+(d.ok?'成功':'失败')+'</b> · '+d.elapsed_ms+'ms';
    await load();
  }catch(e){
    document.getElementById('reloadStatus').textContent='重载失败：'+e;
  }finally{
    btn.disabled=false; btn.textContent='重新加载配置';
  }
}
load(); setInterval(load, 5000);
</script>
</body>
</html>"""
