"""
复果动态上下文数据提供器

按配置中的 `context_providers` 列表，逐个调用外部 HTTP 接口拉取设备侧
上下文（如天气、位置、日程），并格式化为 Markdown 片段注入系统提示词。
每个请求自动附带 `device-id` 头，便于后端做设备级数据隔离。
"""

import asyncio
import httpx
from typing import Dict, Any, List
from fuguoai_config.logger import setup_logging

TAG = __name__


class ContextDataProvider:
    """从配置的外部 API 拉取并格式化动态上下文数据。"""

    def __init__(self, config: Dict[str, Any], logger=None):
        self.config = config
        self.logger = logger or setup_logging()
        self.context_data = ""

    async def _afetch_one(self, client: "httpx.AsyncClient", provider: Dict[str, Any], device_id: str) -> List[str]:
        """拉取单个 provider；任意异常降级为空（不影响其余 provider）。"""
        url = provider.get("url")
        if not url:
            return []

        headers = dict(provider.get("headers", {}) or {})
        headers["device-id"] = device_id

        try:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                self.logger.bind(tag=TAG).warning(f"API {url} 请求失败: {resp.status_code}")
                return []
            payload = resp.json()
            if not isinstance(payload, dict):
                self.logger.bind(tag=TAG).warning(f"API {url} 返回的不是JSON字典")
                return []
            if payload.get("code") != 0:
                self.logger.bind(tag=TAG).warning(f"API {url} 返回错误码: {payload.get('msg')}")
                return []
            return self._format_payload(payload.get("data"))
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"获取上下文数据 {url} 失败: {e}")
            return []

    def fetch_all(self, device_id: str) -> str:
        """并发拉取全部上下文 provider，返回拼接后的 Markdown 字符串。

        演进点（R3）：原实现顺序同步 httpx.get，N 个 provider 延迟累加（N×3s）；
        改为 httpx.AsyncClient + asyncio.gather，延迟降为最慢单个 provider，
        且每个请求受 timeout=3 硬约束，不再因某个 provider 卡死拖垮初始化线程。
        （本方法签名与旧版一致，仅内部实现演进。）
        """
        providers = self.config.get("context_providers", []) or []
        if not providers:
            return ""

        async def _run() -> str:
            async with httpx.AsyncClient(timeout=3) as client:
                tasks = [self._afetch_one(client, p, device_id) for p in providers]
                results = await asyncio.gather(*tasks, return_exceptions=True)
            lines: List[str] = []
            for r in results:
                if isinstance(r, list):
                    lines.extend(r)
                elif isinstance(r, Exception):
                    self.logger.bind(tag=TAG).error(f"上下文 provider 异常: {r}")
            return "\n".join(lines)

        try:
            self.context_data = asyncio.run(_run())
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"拉取上下文数据失败: {e}")
            self.context_data = ""
        if self.context_data:
            self.logger.bind(tag=TAG).debug(f"已注入动态上下文数据:\n{self.context_data}")
        return self.context_data

    @staticmethod
    def _format_payload(data) -> List[str]:
        """把单个接口的 data 字段格式化为 Markdown 行列表。"""
        if isinstance(data, dict):
            return [f"- **{k}：** {v}" for k, v in data.items()]
        if isinstance(data, list):
            return [f"- {item}" for item in data]
        return [f"- {data}"]
