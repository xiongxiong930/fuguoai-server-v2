"""固件 OTA 与接入配置下发（复果自研实现）。

设备在启动时向本接口上报自身型号与当前固件版本；服务端据此在 ``data/bin`` 中
查找更高版本的固件，并把下载地址、WebSocket / MQTT 接入配置一并发回。GET 用于
健康探测与下载入口暴露；``/download`` 负责安全地流式返回固件文件。

本文件为复果独立实现：仅对外契约（响应 JSON 结构、请求头字段名、MQTT topic、
下载安全校验）与既有设备固件保持一致，内部实现、命名与注释均为复果自有。
"""
import base64
import glob
import hashlib
import hmac
import json
import os
import re
import time
from typing import Dict, List, Optional, Tuple

from aiohttp import web

from fuguoai_core.api.base_handler import BaseHandler
from fuguoai_core.auth import AuthManager
from fuguoai_core.utils.util import get_local_ip, get_vision_url

LOG = __name__


# ---------------------------------------------------------------------------
# 纯函数工具（复果自有实现）
# ---------------------------------------------------------------------------
def _only_filename(path: str) -> str:
    """防目录穿越：只保留最后一段文件名。"""
    return os.path.basename(path)


def _version_tuple(version: str) -> Tuple[int, ...]:
    """把版本串解析为可比的数值元组；非数字段忽略。"""
    digits = re.findall(r"\d+", version or "")
    return tuple(int(part) for part in digits) if digits else (0,)


def _version_greater(candidate: str, current: str) -> bool:
    """数值字典序比较 candidate 是否严格大于 current（长度不齐时短侧补 0）。"""
    a, b = _version_tuple(candidate), _version_tuple(current)
    width = max(len(a), len(b))
    for i in range(width):
        x = a[i] if i < len(a) else 0
        y = b[i] if i < len(b) else 0
        if x != y:
            return x > y
    return False


def _firmware_files(bin_dir: str) -> Dict[str, List[Tuple[str, str]]]:
    """扫描 bin 目录，按 ``{model}_{version}.bin`` 命名归并，每个型号按版本降序。"""
    grouped: Dict[str, List[Tuple[str, str]]] = {}
    if not os.path.isdir(bin_dir):
        os.makedirs(bin_dir, exist_ok=True)
        return grouped
    for path in glob.glob(os.path.join(bin_dir, "*.bin")):
        fname = os.path.basename(path)
        match = re.match(r"^(.+?)_([0-9][A-Za-z0-9.\-_]*)\.bin$", fname)
        if not match:
            continue
        grouped.setdefault(match.group(1), []).append((match.group(2), fname))
    for items in grouped.values():
        items.sort(key=lambda entry: _version_tuple(entry[0]), reverse=True)
    return grouped


def _mqtt_password(content: str, secret: str) -> str:
    """用 HMAC-SHA256 给 MQTT 密码签名，返回 urlsafe base64（失败返回空串）。"""
    try:
        digest = hmac.new(
            secret.encode("utf-8"), content.encode("utf-8"), hashlib.sha256
        ).digest()
        return base64.b64encode(digest).decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# 处理器
# ---------------------------------------------------------------------------
class OTAHandler(BaseHandler):
    def __init__(self, config: dict):
        super().__init__(config)
        server = config.get("server", {})
        auth_cfg = server.get("auth", {})
        self.auth_enabled = bool(auth_cfg.get("enabled", False))
        self.allowed_devices = set(auth_cfg.get("allowed_devices", []))
        self.auth = AuthManager(
            secret_key=server.get("auth_key", ""),
            expire_seconds=auth_cfg.get("expire_seconds"),
        )
        self.bin_dir = os.path.join(os.getcwd(), "data", "bin")
        self._firmware_cache: Dict[str, object] = {
            "updated_at": 0,
            "ttl": int(config.get("firmware_cache_ttl", 30)),
            "files_by_model": {},
        }
        # 响应级缓存：同设备(型号+版本)在 TTL 内复用，避免重连刷固件+日志
        self._response_cache: Dict[str, Tuple[float, dict]] = {}
        self._response_cache_ttl = int(config.get("ota_response_cache_ttl", 60))

    # -- 内部辅助 ----------------------------------------------------------
    def _reload_firmware_cache(self) -> None:
        now = int(time.time())
        cache = self._firmware_cache
        if now - int(cache.get("updated_at", 0)) < int(cache.get("ttl", 30)) and cache.get(
            "files_by_model"
        ):
            return
        try:
            cache["files_by_model"] = _firmware_files(self.bin_dir)
            cache["updated_at"] = now
        except Exception as exc:  # noqa: BLE001
            self.logger.bind(tag=LOG).error(f"刷新固件缓存失败: {exc}")

    def _websocket_address(self, local_ip: str, port: int) -> str:
        server = self.config.get("server", {})
        configured = server.get("websocket", "")
        if configured and "你的" not in configured:
            return configured
        return f"ws://{local_ip}:{port}/fuguoai/v1/"

    def _pick_firmware_url(self, model: str, current_version: str) -> Tuple[str, str]:
        """返回 (chosen_version, download_url)；无更新则返回 (current_version, '')。"""
        self._reload_firmware_cache()
        files_by_model = self._firmware_cache.get("files_by_model", {})
        for version, fname in files_by_model.get(model, []):
            if _version_greater(version, current_version):
                base = get_vision_url(self.config)
                url = base.replace("/mcp/vision/explain", f"/fuguoai/ota/download/{fname}")
                return version, url
        return current_version, ""

    def _build_mqtt_section(self, device_id: str, model: str) -> Optional[dict]:
        server = self.config.get("server", {})
        gateway = server.get("mqtt_gateway")
        if not gateway:
            return None
        safe_mac = device_id.replace(":", "_")
        group = f"GID_{model}".replace(":", "_").replace(" ", "_")
        client_id = f"{group}@@@{safe_mac}@@@{safe_mac}"
        try:
            username = base64.b64encode(
                json.dumps({"ip": "unknown"}).encode("utf-8")
            ).decode("utf-8")
        except Exception:  # noqa: BLE001
            username = ""
        signature_key = server.get("mqtt_signature_key", "")
        password = _mqtt_password(f"{client_id}|{username}", signature_key) if signature_key else ""
        if not password:
            self.logger.bind(tag=LOG).warning("缺少MQTT签名密钥，密码留空")
        return {
            "endpoint": gateway,
            "client_id": client_id,
            "username": username,
            "password": password,
            "publish_topic": "device-server",
            "subscribe_topic": f"devices/p2p/{safe_mac}",
        }

    def _build_websocket_section(self, client_id: str, device_id: str, port: int) -> dict:
        token = ""
        if self.auth_enabled:
            if not self.allowed_devices or device_id in self.allowed_devices:
                token = self.auth.generate_token(client_id, device_id)
        return {"url": self._websocket_address(get_local_ip(), port), "token": token}

    def _extract_model_and_version(self, headers, body: dict):
        server = self.config.get("server", {})
        model = ""
        for key in ("device-model", "device_model", "model"):
            if key in headers:
                model = (headers.get(key) or "").strip()
                break
        if not model:
            try:
                if isinstance(body.get("board"), dict):
                    model = body["board"].get("type", "")
                elif "model" in body:
                    model = body.get("model", "")
            except Exception:  # noqa: BLE001
                model = ""
        model = model or "default"

        version = ""
        for key in (
            "device-version",
            "device_version",
            "firmware-version",
            "app-version",
            "application-version",
        ):
            if key in headers:
                version = (headers.get(key) or "").strip()
                break
        if not version:
            try:
                version = body.get("application", {}).get("version", "")
            except Exception:  # noqa: BLE001
                version = ""
        version = version or "0.0.0"
        return model, version, int(server.get("port", 8000)), int(server.get("http_port", 8003))

    # -- 路由入口 ----------------------------------------------------------
    async def handle_post(self, request: web.Request) -> web.Response:
        try:
            device_id = request.headers.get("device-id", "")
            if not device_id:
                raise ValueError("OTA请求设备ID为空")
            client_id = request.headers.get("client-id", "")
            if not client_id:
                raise ValueError("OTA请求ClientID为空")

            raw = await request.text()
            try:
                body = json.loads(raw) if raw else {}
            except Exception:  # noqa: BLE001
                body = {}

            model, version, ws_port, _http_port = self._extract_model_and_version(
                request.headers, body
            )
            server = self.config.get("server", {})

            cache_key = f"{device_id}|{model}|{version}"
            now = time.time()
            hit = self._response_cache.get(cache_key)
            if hit and (now - hit[0]) < self._response_cache_ttl:
                cached = dict(hit[1])
                cached["server_time"] = self._server_time(server)
                response = web.Response(
                    text=json.dumps(cached, separators=(",", ":")),
                    content_type="application/json",
                )
                self._add_cors_headers(response)
                return response

            payload = {
                "name": "复果AI",
                "server_time": self._server_time(server),
                "firmware": {"version": version, "url": ""},
            }
            mqtt = self._build_mqtt_section(device_id, model)
            if mqtt is not None:
                payload["mqtt"] = mqtt
            else:
                payload["websocket"] = self._build_websocket_section(
                    client_id, device_id, ws_port
                )

            chosen_version, url = self._pick_firmware_url(model, version)
            if url:
                payload["firmware"]["version"] = chosen_version
                payload["firmware"]["url"] = url

            self._response_cache[cache_key] = (time.time(), dict(payload))
            response = web.Response(
                text=json.dumps(payload, separators=(",", ":")),
                content_type="application/json",
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.bind(tag=LOG).error(f"OTA POST处理异常: {exc}")
            response = web.Response(
                text=json.dumps({"success": False, "message": "request error."},
                                separators=(",", ":")),
                content_type="application/json",
            )
        finally:
            self._add_cors_headers(response)
            return response

    async def handle_get(self, request: web.Request) -> web.Response:
        try:
            server = self.config.get("server", {})
            local_ip = get_local_ip()
            ws_port = int(server.get("port", 8000))
            payload = {
                "name": "复果AI",
                "websocket": self._websocket_address(local_ip, ws_port),
                "ota": f"http://{local_ip}:{int(server.get('http_port', 8002))}/fuguoai/ota/",
            }
            response = web.Response(
                text=json.dumps(payload, ensure_ascii=False),
                content_type="application/json",
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.bind(tag=LOG).error(f"OTA GET请求异常: {exc}")
            response = web.Response(text="OTA接口异常", content_type="text/plain")
        finally:
            self._add_cors_headers(response)
            return response

    async def handle_download(self, request: web.Request) -> web.StreamResponse:
        try:
            fname = request.match_info.get("filename", "")
            if not fname:
                raise web.HTTPBadRequest(text="filename required")
            fname = _only_filename(fname)
            if not re.match(r"^[A-Za-z0-9.\-_]+\.bin$", fname):
                raise web.HTTPBadRequest(text="invalid filename")

            target = os.path.realpath(os.path.join(self.bin_dir, fname))
            root = os.path.realpath(self.bin_dir)
            if target != root and not target.startswith(root + os.sep):
                raise web.HTTPForbidden(text="forbidden")
            if not os.path.isfile(target):
                raise web.HTTPNotFound(text="file not found")
            response: web.StreamResponse = web.FileResponse(path=target)
        except web.HTTPError as err:
            response = err
        except Exception as exc:  # noqa: BLE001
            self.logger.bind(tag=LOG).error(f"固件下载异常: {exc}")
            response = web.Response(text="download error", status=500)
        finally:
            try:
                self._add_cors_headers(response)
            except Exception:  # noqa: BLE001
                pass
        return response

    @staticmethod
    def _server_time(server: dict) -> dict:
        return {
            "timestamp": int(round(time.time() * 1000)),
            "timezone_offset": int(server.get("timezone_offset", 8)) * 60,
        }
