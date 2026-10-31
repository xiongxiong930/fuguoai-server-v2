"""文旅知识库入库管线（TourismKB ingestion pipeline）。

提供两条入库路径：
- ingest_text()：LLM 辅助路径。原始资料（网页正文/文档）→ LLM 结构化抽取 → 入库。
  对应「资料来源 = LLM 辅助生成」方案。
- ingest_structured()：直灌路径。已结构化的条目列表直接嵌入入库（demo / 批处理用）。

并附带 fetch_public_text()：抓取公开网页正文，作为 ingest_text 的上游数据源。
"""

import re

import requests
from bs4 import BeautifulSoup

from fuguoai_core.utils.tourism_kb import TourismKB

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def fetch_public_text(url, timeout=15):
    """抓取公开网页正文（去脚本样式，拼 <p>/<li> 文本）。失败返回 None。"""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout)
        resp.encoding = resp.apparent_encoding or "utf-8"
        resp.raise_for_status()
    except Exception as e:
        return None, f"fetch failed: {e}"
    try:
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        texts = []
        for node in soup.find_all(["p", "li", "h1", "h2", "h3"]):
            t = node.get_text(strip=True)
            if len(t) >= 8:
                texts.append(t)
        body = "\n".join(texts)
        body = re.sub(r"\n{2,}", "\n", body).strip()
        if len(body) < 50:
            body = soup.get_text(separator="\n", strip=True)
        return body, None
    except Exception as e:
        return None, f"parse failed: {e}"


def ingest_text(kb: TourismKB, kb_id, raw_text, config=None, base_metadata=None):
    """LLM 辅助路径：原始资料 → 结构化抽取 → 入库。返回新增条数。"""
    return kb.ingest_raw(kb_id, raw_text, config=config, base_metadata=base_metadata)


def ingest_structured(kb: TourismKB, kb_id, items):
    """直灌路径：已结构化条目列表直接嵌入入库。返回新增条数。"""
    return kb.ingest_structured(kb_id, items)


def ingest_url(kb: TourismKB, kb_id, url, config=None, base_metadata=None):
    """端到端：抓公开网页 → 抽取 → 入库。返回 (新增条数, 错误信息)。"""
    text, err = fetch_public_text(url)
    if text is None:
        return 0, err
    n = ingest_text(kb, kb_id, text, config=config, base_metadata=base_metadata)
    return n, None
