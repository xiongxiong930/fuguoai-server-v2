"""联网搜索工具（复果自有实现）。

优先走付费搜索源（Metaso / Tavily）；当这些源没结果或无 Key 时，自动降级到
Bing 网页抓取兜底，保证联网搜索始终可用。对外契约（工具名、参数 schema、返回
文案与 Action）保持稳定。
"""

import re
import html
import httpx
from fuguoai_config.logger import setup_logging
from fuguoai_plugins.register import (
    register_function,
    ToolType,
    ActionResponse,
    Action,
)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

_DEFAULT_DESC = "联网搜索工具。当用户明确需要联网搜索问题时使用此工具。"

WEB_SEARCH_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": _DEFAULT_DESC,
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词或问题",
                }
            },
            "required": ["query"],
        },
    },
}


def _strip_html(raw: str) -> str:
    """去掉 HTML 标签、反转义实体并压平空白。"""
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", "", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


async def _query_metaso(key: str, query: str, limit: int) -> str:
    """调用秘塔搜索接口。"""
    url = "https://metaso.cn/api/v1/search"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    body = {
        "q": query,
        "size": limit,
        "stream": False,
        "scope": "webpage",
        "includeSummary": True,
        "includeRawContent": False,
        "conciseSnippet": False,
    }
    logger.bind(tag=TAG).debug(f"秘塔搜索请求 | URL: {url} | payload: {body}")
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0)) as client:
        reply = await client.post(url, json=body, headers=headers)
    data = reply.json()
    logger.bind(tag=TAG).debug(f"秘塔搜索响应 | status: {reply.status_code}")

    pages = data.get("webpages", [])
    if not pages:
        return "未找到相关搜索结果。"

    lines = ["【联网搜索结果】"]
    for idx, page in enumerate(pages, 1):
        lines.append(f"{idx}. 标题：{page.get('title', '无标题')}")
        if page.get("date"):
            lines.append(f"   日期：{page['date']}")
        if page.get("summary"):
            lines.append(f"   摘要：{page['summary']}")
    return "\n".join(lines)


async def _query_tavily(key: str, query: str, limit: int) -> str:
    """调用 Tavily 搜索接口。"""
    url = "https://api.tavily.com/search"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    body = {
        "query": query,
        "max_results": limit,
        "search_depth": "advanced",
        "include_answer": "advanced",
    }
    logger.bind(tag=TAG).debug(f"Tavily搜索请求 | URL: {url} | payload: {body}")
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0)) as client:
        reply = await client.post(url, json=body, headers=headers)
    data = reply.json()
    logger.bind(tag=TAG).debug(f"Tavily搜索响应 | status: {reply.status_code} | data: {data}")

    if not data.get("results"):
        return "未找到相关搜索结果。"

    summary = data.get("answer", "")
    return "\n".join([f"【联网搜索结果】\n总结：{summary}"])


async def _query_bing(key: str, query: str, limit: int) -> str:
    """Bing 网页抓取兜底（无需 Key）。"""
    url = "https://www.bing.com/search"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    params = {"q": query, "count": limit, "setlang": "zh-CN"}
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=True
        ) as client:
            reply = await client.get(url, params=params, headers=headers)
            reply.raise_for_status()
            page_html = reply.text
    except Exception as exc:
        logger.bind(tag=TAG).error(f"Bing 搜索请求失败: {exc}")
        return "未找到相关搜索结果。"

    blocks = re.findall(r'<li class="b_algo".*?</li>', page_html, re.DOTALL)
    lines = ["【联网搜索结果】"]
    shown = 0
    for block in blocks:
        if shown >= limit:
            break
        title_match = re.search(r"<h2[^>]*>.*?<a[^>]*>(.*?)</a>", block, re.DOTALL)
        title = _strip_html(title_match.group(1)) if title_match else "无标题"
        snippet_match = re.search(
            r'class="b_caption"[^>]*>.*?<p[^>]*>(.*?)</p>', block, re.DOTALL
        )
        if not snippet_match:
            snippet_match = re.search(r"<p[^>]*>(.*?)</p>", block, re.DOTALL)
        snippet = _strip_html(snippet_match.group(1)) if snippet_match else ""
        if not title and not snippet:
            continue
        shown += 1
        lines.append(f"{shown}. 标题：{title}")
        if snippet:
            lines.append(f"   摘要：{snippet}")
    if shown == 0:
        return "未找到相关搜索结果。"
    return "\n".join(lines)


@register_function("web_search", WEB_SEARCH_FUNCTION_DESC, ToolType.SYSTEM_CTL)
async def web_search(conn: "ConnectionHandler", query: str = None):
    """按配置好的搜索源做联网检索，无结果时自动降级到 Bing。"""
    logger.bind(tag=TAG).info(f"web_search 被调用 | query={query}")
    if not query:
        return ActionResponse(Action.REQLLM, "请提供搜索关键词。", None)

    cfg = conn.config.get("plugins", {}).get("web_search", {})
    provider = cfg.get("provider", "").lower()
    limit = int(cfg.get("max_results", 3))
    logger.bind(tag=TAG).info(
        f"web_search 配置 | provider={provider} | max_results={limit} | config_keys={list(cfg.keys())}"
    )

    api_key = cfg.get("api_key", "")
    if not api_key:
        return ActionResponse(
            Action.REQLLM,
            "联网搜索功能未配置API Key，请在配置文件中填写。",
            None,
        )

    try:
        if provider == "metaso":
            result_text = await _query_metaso(api_key, query, limit)
        elif provider == "tavily":
            result_text = await _query_tavily(api_key, query, limit)
        elif provider == "bing":
            result_text = await _query_bing(api_key, query, limit)
        else:
            return ActionResponse(
                Action.REQLLM,
                f"联网搜索功能未配置或配置的搜索源无效（当前：{provider}），请检查配置。",
                None,
            )

        # 主源无结果时，自动尝试 Bing 兜底
        if "未找到相关搜索结果" in result_text and provider != "bing":
            logger.bind(tag=TAG).info("主搜索源无结果，自动兜底到 Bing 网页搜索")
            fallback = await _query_bing(api_key, query, limit)
            if "未找到相关搜索结果" not in fallback:
                result_text = fallback
        logger.bind(tag=TAG).info(f"搜索结果组装完成:\n{result_text}")
    except httpx.TimeoutException:
        logger.bind(tag=TAG).error("联网搜索请求超时")
        result_text = "联网搜索请求超时，请稍后重试。"
    except httpx.HTTPStatusError as exc:
        logger.bind(tag=TAG).error(f"联网搜索请求失败: {exc}")
        result_text = "联网搜索请求失败，请稍后重试。"
    except Exception as exc:
        logger.bind(tag=TAG).error(f"联网搜索异常: {exc}")
        result_text = "联网搜索出现异常，请稍后重试。"

    return ActionResponse(Action.REQLLM, result_text, None)
