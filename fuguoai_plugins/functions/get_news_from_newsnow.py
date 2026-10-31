"""新闻聚合工具（复果自有实现）。

从 NewsNow 聚合源拉取热点，或回看上一条新闻的详情。这里只做 HTTP 拉取与
文本拼装，把格式化后的播报稿交给 LLM 念出来。对外契约（工具名、参数 schema、
REQLLM 的播报文案）保持不变。
"""

import random
import httpx
from markitdown import MarkItDown
from fuguoai_config.logger import setup_logging
from fuguoai_plugins.register import register_function, ToolType, ActionResponse, Action
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

# 中文新闻源名 -> NewsNow 的英文源 ID
_SOURCE_IDS = {
    "V2EX": "v2ex-share",
    "知乎": "zhihu",
    "微博": "weibo",
    "联合早报": "zaobao",
    "酷安": "coolapk",
    "MKTNews": "mktnews-flash",
    "华尔街见闻": "wallstreetcn-quick",
    "36氪": "36kr-quick",
    "抖音": "douyin",
    "虎扑": "hupu",
    "百度贴吧": "tieba",
    "今日头条": "toutiao",
    "IT之家": "ithome",
    "澎湃新闻": "thepaper",
    "卫星通讯社": "sputniknewscn",
    "参考消息": "cankaoxiaoxi",
    "远景论坛": "pcbeta-windows11",
    "财联社": "cls-depth",
    "雪球": "xueqiu-hotstock",
    "格隆汇": "gelonghui",
    "法布财经": "fastbull-express",
    "Solidot": "solidot",
    "Hacker News": "hackernews",
    "Product Hunt": "producthunt",
    "Github": "github-trending-today",
    "哔哩哔哩": "bilibili-hot-search",
    "快手": "kuaishou",
    "靠谱新闻": "kaopu",
    "金十数据": "jin10",
    "百度热搜": "baidu",
    "牛客": "nowcoder",
    "少数派": "sspai",
    "稀土掘金": "juejin",
    "凤凰网": "ifeng",
    "虫部落": "chongbuluo-latest",
}

# 配置没指定新闻源时用这一组
DEFAULT_NEWS_SOURCES = "澎湃新闻;百度热搜;财联社"

# 详情缺失/失败时的统一话术
_DETAIL_UNAVAILABLE = "无法获取详细内容"

# 写进工具描述里给模型看的中文来源串
_SAMPLE_SOURCES = DEFAULT_NEWS_SOURCES.replace(";", "、")


def _read_sources(conn) -> str:
    """从配置里取出新闻源字符串（未配置则返回空串）。"""
    node = conn.config.get("plugins", {}).get("get_news_from_newsnow", {})
    raw = node.get("news_sources", "")
    return raw.strip() if isinstance(raw, str) else ""


def _resolve_source_list(conn) -> str:
    """取新闻源配置，缺失或异常时退回默认组。"""
    try:
        configured = _read_sources(conn)
        if configured:
            logger.bind(tag=TAG).debug(f"使用配置的新闻源: {configured}")
            return configured
        logger.bind(tag=TAG).debug("未找到新闻源配置，使用默认配置")
        return DEFAULT_NEWS_SOURCES
    except Exception as exc:
        logger.bind(tag=TAG).error(f"获取新闻源配置失败: {exc}，使用默认配置")
        return DEFAULT_NEWS_SOURCES


async def _fetch_headlines(conn: "ConnectionHandler", source_id: str) -> list:
    """从 NewsNow 接口拉取某源的新闻条目。"""
    base = conn.config.get("plugins", {}).get("get_news_from_newsnow", {}).get("url")
    url = f"{base}{source_id}" if base else f"https://newsnow.busiyi.world/api/s?id={source_id}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
            reply = await client.get(url, headers=headers)
        payload = reply.json()
        return payload.get("items", [])
    except Exception as exc:
        logger.bind(tag=TAG).error(f"获取新闻API失败: {exc}")
        return []


async def _fetch_article(url: str) -> str:
    """抓取详情页并用 MarkItDown 洗成纯文本。"""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
            reply = await client.get(url, headers=headers)
        converter = MarkItDown(enable_plugins=False)
        extracted = converter.convert(reply).text_content
        if not extracted or not extracted.strip():
            logger.bind(tag=TAG).warning(f"清理后的新闻内容为空: {url}")
            return "无法解析新闻详情内容，可能是网站结构特殊或内容受限。"
        return extracted
    except Exception as exc:
        logger.bind(tag=TAG).error(f"获取新闻详情失败: {exc}")
        return _DETAIL_UNAVAILABLE


def _report_detail(title: str, lang: str, detail_content: str) -> str:
    return (
        f"根据下列数据，用{lang}回应用户的新闻详情查询请求：\n\n"
        f"新闻标题: {title}\n"
        f"详细内容: {detail_content}\n\n"
        f"(请对上述新闻内容进行总结，提取关键信息，以自然、流畅的方式向用户播报，"
        f"不要提及这是总结，就像是在讲述一个完整的新闻)"
    )


def _report_headline(title: str, lang: str) -> str:
    return (
        f"根据下列数据，用{lang}回应用户的新闻查询请求：\n\n"
        f"新闻标题: {title}\n"
        f"(请以自然、流畅的方式向用户播报这条新闻标题，"
        f"提示用户可以要求获取详细内容，此时会获取新闻的详细内容。)"
    )


GET_NEWS_FROM_NEWSNOW_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "get_news_from_newsnow",
        "description": "当用户要求查看或收听新闻时调用（如'来条新闻''今天有什么新闻'）。",
        "parameters": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": f"新闻源的标准中文名称，例如{_SAMPLE_SOURCES}等。可选参数，如果不提供则使用默认新闻源",
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


@register_function(
    "get_news_from_newsnow",
    GET_NEWS_FROM_NEWSNOW_FUNCTION_DESC,
    ToolType.SYSTEM_CTL,
)
async def get_news_from_newsnow(
    conn: "ConnectionHandler",
    source: str = "澎湃新闻",
    detail: bool = False,
    lang: str = "zh_CN",
):
    """取一条新闻播报，或回看上一条新闻的详情。"""
    try:
        if str(detail).lower() == "true":
            prior = getattr(conn, "last_newsnow_link", None)
            if not prior or "url" not in prior:
                return ActionResponse(
                    Action.REQLLM,
                    "抱歉，没有找到最近查询的新闻，请先获取一条新闻。",
                    None,
                )

            url = prior.get("url")
            title = prior.get("title", "未知标题")
            source_id = prior.get("source_id", "thepaper")
            if not url or url == "#":
                return ActionResponse(
                    Action.REQLLM, "抱歉，该新闻没有可用的链接获取详细内容。", None
                )

            logger.bind(tag=TAG).debug(
                f"获取新闻详情: {title}, 来源: {source_id}, URL={url}"
            )
            detail_content = await _fetch_article(url)
            if not detail_content or detail_content == _DETAIL_UNAVAILABLE:
                return ActionResponse(
                    Action.REQLLM,
                    f"抱歉，无法获取《{title}》的详细内容，可能是链接已失效或网站结构发生变化。",
                    None,
                )
            return ActionResponse(
                Action.REQLLM, _report_detail(title, lang, detail_content), None
            )

        # 正常路径：列源、随机挑一条
        sources = _resolve_source_list(conn)
        configured = [name.strip() for name in sources.split(";") if name.strip()]
        channel_id = _SOURCE_IDS.get(source) if source in configured else None
        if not channel_id:
            logger.bind(tag=TAG).warning(f"无效的新闻源: {source}，使用默认源澎湃新闻")
            channel_id, source = "thepaper", "澎湃新闻"

        logger.bind(tag=TAG).info(f"获取新闻: 新闻源={source}({channel_id})")
        items = await _fetch_headlines(conn, channel_id)
        if not items:
            return ActionResponse(
                Action.REQLLM,
                f"抱歉，未能从{source}获取到新闻信息，请稍后再试或尝试其他新闻源。",
                None,
            )

        chosen = random.choice(items)
        if not hasattr(conn, "last_newsnow_link"):
            conn.last_newsnow_link = {}
        conn.last_newsnow_link = {
            "url": chosen.get("url", "#"),
            "title": chosen.get("title", "未知标题"),
            "source_id": channel_id,
        }
        return ActionResponse(
            Action.REQLLM, _report_headline(chosen["title"], lang), None
        )
    except Exception as exc:
        logger.bind(tag=TAG).error(f"获取新闻出错: {exc}")
        return ActionResponse(
            Action.REQLLM, "抱歉，获取新闻时发生错误，请稍后再试。", None
        )
