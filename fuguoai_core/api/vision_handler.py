"""视觉理解接口处理器（复果原生实现，clean-room 重写）。

接收设备上传的图片与问题，调用 vLLM 视觉模型生成描述后回传。
提供 POST（推理）与 GET（健康检查）两个端点，统一附加 CORS 头。

本文件与上游 MIT 源仅共享 aiohttp / BaseHandler / vLLM 的调用契约，
控制流、变量命名、错误文案均为独立重写。
"""

import base64
import copy
import json
from typing import Optional, Tuple

from aiohttp import web

from fuguoai_config.config_loader import get_private_config_from_api
from fuguoai_config.logger import setup_logging
from fuguoai_core.api.base_handler import BaseHandler
from fuguoai_core.utils.auth import AuthToken
from fuguoai_core.utils.util import get_vision_url, is_valid_image_file
from fuguoai_core.utils.vllm import create_instance
from fuguoai_plugins.register import Action

_LOG = setup_logging()
_LIMIT = 5 * 1024 * 1024  # 单图上限 5MB


class VisionHandler(BaseHandler):
    """视觉问答 HTTP 处理器。"""

    def __init__(self, config: dict):
        super().__init__(config)
        self.auth = AuthToken(config["server"]["auth_key"])

    def _err(self, message: str) -> dict:
        """统一错误响应结构。"""
        return {"success": False, "message": message}

    def _check_token(self, request) -> Tuple[bool, Optional[str]]:
        """校验 Authorization Bearer；测试客户端可免鉴权。"""
        header = request.headers.get("Authorization", "")
        client = request.headers.get("Client-Id", "")
        if client == "web_test_client":
            return True, request.headers.get("Device-Id", "test_device")
        if not header.startswith("Bearer "):
            return False, None
        return self.auth.verify_token(header[7:])

    async def _read_parts(self, request):
        """解析 multipart：依次读问题字段与图片字段，返回 (问题, base64 图)。"""
        reader = await request.multipart()
        q_field = await reader.next()
        if q_field is None:
            raise ValueError("未收到问题字段")
        question = await q_field.text()
        img_field = await reader.next()
        if img_field is None:
            raise ValueError("未收到图片字段")
        raw = await img_field.read()
        if not raw:
            raise ValueError("图片内容为空")
        if len(raw) > _LIMIT:
            raise ValueError(f"图片超出大小上限，最大允许 {_LIMIT // 1024 // 1024}MB")
        if not is_valid_image_file(raw):
            raise ValueError("文件格式不受支持，请上传 JPEG / PNG / GIF / BMP / TIFF / WEBP 之一")
        return question, base64.b64encode(raw).decode("utf-8")

    def _pick_model(self, cfg):
        """按配置选出视觉模型实例。"""
        name = cfg["selected_module"].get("VLLM")
        if not name:
            raise ValueError("尚未配置默认视觉分析模块")
        spec = cfg["VLLM"][name]
        kind = name if "type" not in spec else spec["type"]
        if not kind:
            raise ValueError(f"无法定位 VLLM 供应器：{kind}")
        return create_instance(kind, spec)

    async def handle_post(self, request):
        """处理视觉理解 POST：校验 -> 解析 -> 推理 -> 回包。"""
        resp = None
        try:
            ok, device_id = self._check_token(request)
            if not ok:
                resp = web.Response(
                    text=json.dumps(self._err("认证令牌无效或已过期")),
                    content_type="application/json",
                    status=401,
                )
                return resp

            claimed = request.headers.get("Device-Id", "")
            if claimed != device_id:
                raise ValueError("设备标识与令牌不一致")

            question, image_b64 = await self._read_parts(request)

            cfg = copy.deepcopy(self.config)
            if cfg.get("read_config_from_api", False):
                cfg = await get_private_config_from_api(
                    cfg, device_id, request.headers.get("Client-Id", "")
                )

            model = self._pick_model(cfg)
            answer = await model.response(question, image_b64)

            resp = web.Response(
                text=json.dumps(
                    {"success": True, "action": Action.RESPONSE.name, "response": answer},
                    separators=(",", ":"),
                ),
                content_type="application/json",
            )
        except ValueError as e:
            _LOG.bind(tag=__name__).error(f"视觉 POST 处理失败: {e}")
            resp = web.Response(
                text=json.dumps(self._err(str(e)), separators=(",", ":")),
                content_type="application/json",
            )
        except Exception as e:
            _LOG.bind(tag=__name__).error(f"视觉 POST 处理失败: {e}")
            resp = web.Response(
                text=json.dumps(self._err("服务处理请求时发生异常"), separators=(",", ":")),
                content_type="application/json",
            )
        finally:
            if resp is not None:
                self._add_cors_headers(resp)
            return resp

    async def handle_get(self, request):
        """健康检查：回显视觉解释接口地址是否就绪。"""
        try:
            url = get_vision_url(self.config)
            if url and url != "null":
                text = f"视觉接口运行正常，解释服务地址：{url}"
            else:
                text = "视觉接口未就绪，请在 data/.config.yaml 中配置 server.vision_explain 地址"
            resp = web.Response(text=text, content_type="text/plain")
        except Exception as e:
            _LOG.bind(tag=__name__).error(f"视觉 GET 处理失败: {e}")
            resp = web.Response(
                text=json.dumps(self._err("服务内部错误"), separators=(",", ":")),
                content_type="application/json",
            )
        finally:
            if resp is not None:
                self._add_cors_headers(resp)
            return resp
