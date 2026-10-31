"""设备 WebSocket 接入层（复果原生实现）。

职责：把裸 WS 连接做标识解析 → 鉴权 → 连接风暴熔断 → 交给 ConnectionHandler
进入对话生命周期。

本协议契约由后端定义，固件侧对齐此实现；现有线协议字段（device-id / client-id /
authorization / role=phone、upgrade 握手、1013 风暴关闭码、"Server is running" 就绪响应）
保持不变以兼容在网设备。

M7 设备级鉴权、连接风暴熔断、B-5 phone 角色、在线状态离线判定均为 fuguoai 自有演进。
"""

import asyncio
import logging
import time
from urllib.parse import parse_qs, urlparse

import websockets
from fuguoai_config.logger import setup_logging

from fuguoai_core.connection import ConnectionHandler
from fuguoai_config.config_loader import get_config_from_api_async
from fuguoai_core.auth import AuthManager, AuthenticationError
from fuguoai_core.utils.modules_initialize import initialize_modules
from fuguoai_core.utils.util import check_vad_update, check_asr_update, normalize_device_id

TAG = __name__

# ---- 协议契约常量（固件侧对齐此实现，勿擅自改字符串）----
_READY_BANNER = "Server is running\n"
_AUTH_REJECT_NOTICE = "认证失败"
_PROBE_HINT = "端口正常，如需测试连接，请启动digital-human测试"
_STORM_CLOSE_CODE = 1013
_STORM_CLOSE_REASON = "try again later"
_OFFLINE_GRACE_SECONDS = 60
_OFFLINE_SCAN_INTERVAL = 30


class HandshakeNoiseFilter(logging.Filter):
    """屏蔽无效握手产生的噪声日志（如用 HTTPS 访问 WS 端口、裸 TCP 探测）。"""

    _NOISE_KEYWORDS = (
        "opening handshake failed",
        "did not receive a valid HTTP request",
        "connection closed while reading HTTP request",
        "line without CRLF",
    )

    def filter(self, record):
        msg = record.getMessage()
        return not any(kw in msg for kw in self._NOISE_KEYWORDS)


def _install_handshake_noise_filter():
    """给 websockets 系列 logger 挂上无效握手过滤器，避免握手噪声刷屏。"""
    flt = HandshakeNoiseFilter()
    for name in ("websockets", "websockets.server", "websockets.client"):
        logging.getLogger(name).addFilter(flt)


_install_handshake_noise_filter()


class WebSocketServer:
    """WebSocket 接入层：连接分拣 → 鉴权 → 风暴熔断 → 交给 ConnectionHandler。"""

    def __init__(self, config: dict):
        self.config = config
        self.logger = setup_logging(config)
        self.config_lock = asyncio.Lock()
        self._load_modules(config)
        self._load_auth(config)
        self._load_device_ws_auth(config)

    # ---- 初始化拆件 ----

    def _load_modules(self, config: dict):
        """按配置启用的模块做一次性初始化；未启用置 None（下游按需判空）。"""
        sel = config["selected_module"]
        modules = initialize_modules(
            self.logger,
            config,
            "VAD" in sel,
            "ASR" in sel,
            "LLM" in sel,
            False,
            "Memory" in sel,
            "Intent" in sel,
        )
        self._vad = modules.get("vad")
        self._asr = modules.get("asr")
        self._llm = modules.get("llm")
        self._intent = modules.get("intent")
        self._memory = modules.get("memory")

    def _load_auth(self, config: dict):
        """既有 auth_key 机制（兼容老设备 JWT）。"""
        auth_config = config["server"].get("auth", {})
        self.auth_enable = auth_config.get("enabled", False)
        self.allowed_devices = set(auth_config.get("allowed_devices", []))
        secret_key = config["server"]["auth_key"]
        expire_seconds = auth_config.get("expire_seconds", None)
        self.auth = AuthManager(secret_key=secret_key, expire_seconds=expire_seconds)

    def _load_device_ws_auth(self, config: dict):
        """M7：设备级 WS 鉴权（与 auth_key 相互独立，任一通过即放行）。默认关闭。"""
        device_ws_auth_config = (config["server"] or {}).get("device_ws_auth", {})
        self.device_ws_auth_enable = bool(device_ws_auth_config.get("enabled", False))
        # v3.0 双认：允许固件在握手后发 JSON auth 消息补全鉴权（替代 header token）。
        # 默认关闭 → 缺失 header token 仍按 legacy 在握手阶段拒绝；开启后放行到
        # AuthTextMessageHandler 校验。对未启用该选项的机器零行为变化。
        self.device_ws_auth_allow_json = bool(
            device_ws_auth_config.get("allow_json_auth", False)
        )

    # ---- 服务生命周期 ----

    async def start(self):
        server_config = self.config["server"]
        host = server_config.get("ip", "0.0.0.0")
        port = int(server_config.get("port", 8000))

        # 在线状态持久化：建表 + 离线判定定时器（旁路，失败不影响服务）
        try:
            from fuguoai_core.native.device_store import init_schema

            await asyncio.to_thread(init_schema)
            asyncio.create_task(self._offline_judge_loop())
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"在线状态初始化失败(已忽略): {e}")

        async with websockets.serve(
            self._handle_connection, host, port, process_request=self._http_response
        ):
            await asyncio.Future()

    async def _offline_judge_loop(self):
        """30s 一轮：last_seen 距今 > 60s 且仍 online → 置 offline。

        兜底「连接未干净关闭（断网 / 容器重启）导致 _save_and_close 未执行」的场景，
        避免设备列表长期显示在线。绝不进入对话 / TTS 路径。
        """
        while True:
            await asyncio.sleep(_OFFLINE_SCAN_INTERVAL)
            cutoff = int(time.time()) - _OFFLINE_GRACE_SECONDS
            try:
                from fuguoai_core.native.device_store import offline_stale_async

                await offline_stale_async(cutoff)
            except Exception as e:
                self.logger.bind(tag=TAG).error(f"offline judge err: {e}")

    # ---- 单连接处理 ----

    async def _handle_connection(self, websocket: websockets.ServerConnection):
        # 设备标识优先取 header；缺失时回退到 URL 查询参数
        headers = dict(websocket.request.headers)
        if headers.get("device-id", None) is None:
            if not await self._resolve_identity_from_query(websocket):
                return

        # 先认证，后建连
        try:
            await self._handle_auth(websocket)
        except AuthenticationError:
            await websocket.send(_AUTH_REJECT_NOTICE)
            await websocket.close()
            return

        # 连接风暴熔断（2026-08-01 e268 崩溃循环）：设备固件崩溃后每 ~3s 重连一次，
        # 每次都触发一整套 LLM/ASR/TTS/提示词初始化。此处在创建 ConnectionHandler
        # 之前拦截，冷却期内直接以标准关闭码 1013 关闭，不做任何初始化。
        # 保守设计：仅对能明确归属的设备生效（phone 端与无 device-id 一律放行），
        # 冷却到期自动放行自愈，绝不永久封禁；可经 config.conn_storm_guard.enabled 关闭。
        if not await self._maybe_block_storm(websocket):
            return

        # 创建 ConnectionHandler（最后一位传入 server 自身，供其内部回调）
        handler = ConnectionHandler(
            self.config,
            self._vad,
            self._asr,
            self._llm,
            self._memory,
            self._intent,
            self,
        )
        try:
            await handler.handle_connection(websocket)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"处理连接时出错: {e}")
        finally:
            # 强制关闭连接（若尚未关闭）
            try:
                if hasattr(websocket, "closed") and not websocket.closed:
                    await websocket.close()
                elif hasattr(websocket, "state") and websocket.state.name != "CLOSED":
                    await websocket.close()
                else:
                    await websocket.close()
            except Exception as close_error:
                self.logger.bind(tag=TAG).error(
                    f"服务器端强制关闭连接时出错: {close_error}"
                )

    async def _resolve_identity_from_query(self, websocket) -> bool:
        """从 URL 查询参数补全设备标识；无法满足握手条件则关闭并返回 False。"""
        request_path = websocket.request.path
        if not request_path:
            self.logger.bind(tag=TAG).error("无法获取请求路径")
            return False
        parsed = urlparse(request_path)
        query = parse_qs(parsed.query)

        # B-5 模式B（契约 v1.9）：小程序 phone 端无 device-id 头，URL 带
        # ?role=phone 标记。此处仅放行握手，真正认证在 hello 内用用户 JWT
        # 完成（helloHandle._authenticate_and_register_phone），认证失败即关。
        if query.get("role", [""])[0] == "phone":
            websocket.request.headers["x-role"] = "phone"
        elif "device-id" not in query:
            # v3.0 双认：允许无 device-id 的 WS 连接进入握手，device-id 由后续
            # JSON auth 消息补全；不再把无头 WS 当探针拒绝（固件可零头连接）。
            return True
        else:
            websocket.request.headers["device-id"] = query["device-id"][0]

        if "client-id" in query:
            websocket.request.headers["client-id"] = query["client-id"][0]
        if "authorization" in query:
            websocket.request.headers["authorization"] = query["authorization"][0]
        return True

    async def _reject_probe(self, websocket) -> bool:
        """裸 TCP / 非设备探测：提示后关闭，不入对话。"""
        await websocket.send(_PROBE_HINT)
        await websocket.close()
        return False

    async def _maybe_block_storm(self, websocket) -> bool:
        """连接风暴熔断检查；返回 True 表示放行，False 表示已拦截关闭。"""
        try:
            cfg = (self.config or {}).get("conn_storm_guard") or {}
            hdrs = dict(websocket.request.headers)
            if not (cfg.get("enabled", True) and hdrs.get("x-role") != "phone"):
                return True
            dev = normalize_device_id(hdrs.get("device-id"))
            if not dev:
                return True
            from fuguoai_core.utils.conn_guard import get_conn_storm_guard

            guard = get_conn_storm_guard()
            guard.configure(cfg)
            ok, _why = guard.allow(dev)
            if not ok:
                await websocket.close(_STORM_CLOSE_CODE, _STORM_CLOSE_REASON)
                return False
        except Exception as err:
            # 熔断器自身异常绝不影响正常建连
            self.logger.bind(tag=TAG).warning(f"连接风暴熔断检查异常(已放行): {err}")
        return True

    async def _http_response(self, websocket, request_headers):
        # WebSocket 升级请求 → 返回 None 放行握手；普通 HTTP → 返回就绪文本
        if request_headers.headers.get("connection", "").lower() == "upgrade":
            return None
        return websocket.respond(200, _READY_BANNER)

    async def update_config(self) -> bool:
        """热更新服务器配置并重新初始化受影响的组件。

        Returns:
            bool: 更新是否成功
        """
        try:
            async with self.config_lock:
                new_config = await get_config_from_api_async(self.config)
                if new_config is None:
                    self.logger.bind(tag=TAG).error("获取新配置失败")
                    return False
                self.logger.bind(tag=TAG).info(f"获取新配置成功")

                update_vad = check_vad_update(self.config, new_config)
                update_asr = check_asr_update(self.config, new_config)
                self.logger.bind(tag=TAG).info(
                    f"检查VAD和ASR类型是否需要更新: {update_vad} {update_asr}"
                )

                self.config = new_config
                self._load_modules(new_config)
                self.logger.bind(tag=TAG).info(f"更新配置任务执行完毕")
                return True
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"更新服务器配置失败: {str(e)}")
            return False

    async def _handle_auth(self, websocket: websockets.ServerConnection):
        # 先认证，后建连
        # M7：设备级 WS 鉴权（device_ws_auth）与既有 auth_key 机制相互独立，任一通过即放行
        if not (self.auth_enable or self.device_ws_auth_enable):
            return

        headers = dict(websocket.request.headers)
        # B-5 模式B：phone 端握手放行，认证延迟到 hello（用户 JWT 验签，
        # 失败回 auth_failed 并关连接；P0-3 握手超时兜底防挂空连接）。
        if headers.get("x-role") == "phone":
            return
        # v3.0 双认：若显式开启 allow_json_auth，缺失 header token 不在此拒绝，
        # 放行到连接层由 AuthTextMessageHandler 用 JSON auth 消息校验。
        if self.device_ws_auth_allow_json and self.device_ws_auth_enable:
            token = (headers.get("authorization", "") or "").strip()
            if not token:
                return
        device_id = headers.get("device-id", None)
        client_id = headers.get("client-id", None)
        token = (headers.get("authorization", "") or "").strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()

        # 白名单设备：始终放行（兼顾老设备如 c3，避免误杀）
        if self.allowed_devices and device_id in self.allowed_devices:
            return

        # 1) 既有 auth_key JWT 校验（兼容老设备）
        if self.auth_enable and token:
            if self.auth.verify_token(token, client_id=client_id, username=device_id):
                return
            if not self.device_ws_auth_enable:
                raise AuthenticationError("Invalid token")

        # 2) M7 设备级 token 校验：token 必须与 device_id 绑定且设备已激活（防冒充 device-id）
        if self.device_ws_auth_enable:
            # v3.0 双认：缺 header device-id / token 时放行到连接层，由 AuthTextMessageHandler
            # 用 JSON auth 消息补全校验，不在此处拒绝（固件可零头连接）。
            if not device_id or not token:
                return
            from fuguoai_core.device_access import DeviceBindingStore

            # device-id 归一化（去冒号/连字符+小写）以匹配库中规范形态，
            # 否则 WS 固件带冒号格式(94:a9:90:..)与库无冒号(94a990..)不一致导致校验失败
            if DeviceBindingStore().verify_device_token(
                normalize_device_id(device_id), token
            ):
                return
            raise AuthenticationError("Invalid or unbound device token")
