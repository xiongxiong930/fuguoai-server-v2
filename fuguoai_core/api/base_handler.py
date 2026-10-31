"""
复果 HTTP API 基础处理器

所有 REST 路由处理器的公共基类，负责统一注入 CORS 响应头并处理
浏览器预检（OPTIONS）请求，避免每个路由重复样板代码。
"""

from aiohttp import web
from fuguoai_config.logger import setup_logging


class BaseHandler:
    """API 处理器基类：提供 CORS 头注入与 OPTIONS 预检处理。"""

    def __init__(self, config: dict):
        self.config = config
        self.logger = setup_logging()

    def _add_cors_headers(self, response: web.Response) -> None:
        """为响应补充跨域允许头。"""
        response.headers["Access-Control-Allow-Headers"] = (
            "client-id, content-type, device-id, authorization"
        )
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Origin"] = "*"

    async def handle_options(self, request: web.Request) -> web.Response:
        """处理 OPTIONS 预检请求，返回允许的方法与 CORS 头。"""
        response = web.Response(body=b"", content_type="text/plain")
        self._add_cors_headers(response)
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return response
