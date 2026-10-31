"""中新网新闻工具（复果自有实现）。

从中新网 RSS 取头条，或回看上一条新闻的正文详情。这里只负责抓取与文本拼装，
格式化后的播报稿交给 LLM 念出。对外契约（工具名、参数 schema、REQLLM 文案）不变。
"""

import random
import httpx
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from fuguoai_config.logger import setup_logging
from fuguoai_plugins.register import register_function, ToolType, ActionResponse, Action
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

GET_NEWS_FROM_CHINANEWS_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "get_news_from_chinanews",
        "description": (
            "当用户要求查看或收听新闻时调用（如'来条新闻''今天有什么新闻'）。"
            "用户可以指定新闻类型，如社会新闻、科技新闻、国际新闻等。"
            "如果没有指定，默认播报社会新闻。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "新闻类别，例如社会、科技、国际。可选参数，如果不提供则使用默认类别",
                },
                "detail": {
                    "type": "boolean",
                    "description": "是否获取详细内容，默认为false。如果为true，则获取上一条新闻的详细内容",
                },
                "lang": {
                    "type": "string",
                    "description": "返回用户使用的语言code，例如zh_CN/zh_HK/en_US/ja_JP等，默认zh_CN",
                },
            },
            "required": ["lang"],
        },
    },
}

# 找不到正文容器时，整页正文最多取这么多字
_FALLBACK_TEXT_LIMIT = 2000

# 详情抓取失败时的统一话术
_DETAIL_UNAVAILABLE = "无法获取详细内容"

# 类别映射：中文类别 -> 配置里的 RSS 键
_CATEGORY_LOOKUP = {
    "社会": "society_rss_url",
    "社会新闻": "society_rss_url",
    "国际": "world_rss_url",
    "国际新闻": "world_rss_url",
    "财经": "finance_rss_url",
    "财经新闻": "finance_rss_url",
    "金融": "finance_rss_url",
    "经济": "finance_rss_url",
}


def _read_rss(content: bytes) -> list:
    """把 RSS XML 解析成新闻条目列表。"""
    root = ET.fromstring(content)
    entries = []
    for item in root.findall(".//item"):
        entries.append(
            {
                "title": item.findtext("title") or "无标题",
                "link": item.findtext("link") or "#",
                "description": item.findtext("description") or "无描述",
                "pubDate": item.findtext("pubDate") or "未知时间",
            }
        )
    return entries


async def _pull_rss(rss_url: str) -> list:
    """从指定 RSS 地址拉取新闻条目。"""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
            reply = await client.get(rss_url)
        return _read_rss(reply.content)
    except Exception as exc:
        logger.bind(tag=TAG).error(f"获取RSS新闻失败: {exc}")
        return []


def _read_article(content: bytes) -> str:
    """从 HTML 里掏正文段落。"""
    soup = BeautifulSoup(content, "html.parser")
    container = soup.select_one(".content_desc, .content, article, .article-content")
    if container:
        paragraphs = container.find_all("p")
    else:
        paragraphs = soup.find_all("p")
    text = "\n".join(p.get_text().strip() for p in paragraphs if p.get_text().strip())
    return text[:_FALLBACK_TEXT_LIMIT]


async def _pull_detail(url: str) -> str:
    """抓取新闻详情页并提取正文。"""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
            reply = await client.get(url)
        return _read_article(reply.content)
    except Exception as exc:
        logger.bind(tag=TAG).error(f"获取新闻详情失败: {exc}")
        return _DETAIL_UNAVAILABLE


def _match_category(category_text):
    """把用户输入的中文类别映射到配置里的类别键（无匹配则原样返回）。"""
    if not category_text:
        return None
    return _CATEGORY_LOOKUP.get(category_text.lower().strip(), category_text)


def _compose_detail(title: str, lang: str, detail_content: str) -> str:
    return (
        f"根据下列数据，用{lang}回应用户的新闻详情查询请求：\n\n"
        f"新闻标题: {title}\n"
        f"详细内容: {detail_content}\n\n"
        f"(请对上述新闻内容进行总结，提取关键信息，以自然、流畅的方式向用户播报，"
        f"不要提及这是总结，就像是在讲述一个完整的新闻故事)"
    )


def _compose_headline(selected_news: dict, lang: str) -> str:
    return (
        f"根据下列数据，用{lang}回应用户的新闻查询请求：\n\n"
        f"新闻标题: {selected_news['title']}\n"
        f"发布时间: {selected_news['pubDate']}\n"
        f"新闻内容: {selected_news['description']}\n"
        f"(请以自然、流畅的方式向用户播报这条新闻，可以适当总结内容，"
        f"直接读出新闻即可，不需要额外多余的内容。"
        f"如果用户询问更多详情，告知用户可以说'请详细介绍这条新闻'获取更多内容)"
    )


@register_function(
    "get_news_from_chinanews",
    GET_NEWS_FROM_CHINANEWS_FUNCTION_DESC,
    ToolType.SYSTEM_CTL,
)
async def get_news_from_chinanews(
    conn: "ConnectionHandler",
    category: str = None,
    detail: bool = False,
    lang: str = "zh_CN",
):
    """取一条中新网新闻播报，或回看上一条新闻的详情。"""
    try:
        if detail:
            prior = getattr(conn, "last_news_link", None)
            if not prior or "link" not in prior:
                return ActionResponse(
                    Action.REQLLM,
                    "抱歉，没有找到最近查询的新闻，请先获取一条新闻。",
                    None,
                )

            link = prior.get("link")
            title = prior.get("title", "未知标题")
            if link == "#":
                return ActionResponse(
                    Action.REQLLM, "抱歉，该新闻没有可用的链接获取详细内容。", None
                )

            logger.bind(tag=TAG).debug(f"获取新闻详情: {title}, URL={link}")
            detail_content = await _pull_detail(link)
            if not detail_content or detail_content == _DETAIL_UNAVAILABLE:
                return ActionResponse(
                    Action.REQLLM,
                    f"抱歉，无法获取《{title}》的详细内容，可能是链接已失效或网站结构发生变化。",
                    None,
                )
            return ActionResponse(
                Action.REQLLM, _compose_detail(title, lang, detail_content), None
            )

        # 正常路径：定位 RSS 地址并随机挑一条
        rss_cfg = conn.config.get("plugins", {}).get("get_news_from_chinanews", {})
        default_url = rss_cfg.get(
            "default_rss_url", "https://www.chinanews.com.cn/rss/society.xml"
        )

        mapped = _match_category(category)
        rss_url = default_url
        if mapped and mapped in rss_cfg:
            rss_url = rss_cfg[mapped]

        logger.bind(tag=TAG).info(
            f"获取新闻: 原始类别={category}, 映射类别={mapped}, URL={rss_url}"
        )

        items = await _pull_rss(rss_url)
        if not items:
            return ActionResponse(
                Action.REQLLM, "抱歉，未能获取到新闻信息，请稍后再试。", None
            )

        picked = random.choice(items)
        if not hasattr(conn, "last_news_link"):
            conn.last_news_link = {}
        conn.last_news_link = {
            "link": picked.get("link", "#"),
            "title": picked.get("title", "未知标题"),
        }
        return ActionResponse(
            Action.REQLLM, _compose_headline(picked, lang), None
        )
    except Exception as exc:
        logger.bind(tag=TAG).error(f"获取新闻出错: {exc}")
        return ActionResponse(
            Action.REQLLM, "抱歉，获取新闻时发生错误，请稍后再试。", None
        )
