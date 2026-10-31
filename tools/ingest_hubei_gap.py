# -*- coding: utf-8 -*-
"""湖北文旅缺口补完（Phase H，2026-09-24）：神农架/荆州/襄阳/木兰/土家女儿城/明显陵 的
逸闻故事与诗词素材入库。

数据源（均为 159 容器实测可达；探测脚本 /tmp/probe_gap_hubei.py / probe_baike.py 已确认）：
  A. 古诗文网《木兰诗》(gushiwen GuShiWen_d075b40e82) → type=poem（原文+译文，18843字）
  B. 搜狐旅游攻略（荆州/襄阳/明显陵/土家女儿城）：标题营销腔重，但正文含楚文化/三国/
     诸葛亮/土家习俗/名人典故等真实历史背景 → type=culture
  C. 搜狗百科「土家族」：干净的少数民族百科正文 → type=culture
  D. 网易订阅 神农架游记（13 条真实感受，约 7100 字，导航块已过滤）→ type=culture

为何不用百度百科 / 政府门户：
  - 百度百科对容器请求统一 403（反爬），不可用；
  - 各 gov.cn 首页是门户（新闻+导航+跳转墙），抽不出景点典故正文。

注意：type 必须落在 tourism_kb._KB_TYPES 白名单（culture/poem 已 2026-09-23 新增）。
改磁盘后必须 docker restart，HTTP 端点才看得到（进程持有内存缓存）。
"""
import asyncio
import os
import re
import sys
import urllib.parse
from collections import defaultdict

import requests
from bs4 import BeautifulSoup

# 定位仓库根：脚本可能放仓库 tools/ 或临时丢 /tmp 在容器跑。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not os.path.isdir(os.path.join(_ROOT, "fuguoai_core")):
    _ROOT = os.environ.get("FUGUOAI_ROOT", "/opt/fuguoai-server")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fuguoai_core.utils.tourism_kb import get_tourism_kb  # noqa: E402
from fuguoai_config.config_loader import load_config  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# (city, spot, url, type, source_site)  —— 晴川阁无可达源，本轮不收。
SOURCES = [
    ("湖北", "神农架", "https://www.163.com/dy/article/JF2E6KS70524CUS3.html",
     "culture", "网易订阅游记"),
    ("荆州", "荆州古城", "https://www.sohu.com/a/932452560_121893286",
     "culture", "搜狐旅游"),
    ("襄阳", "襄阳古城", "https://www.sohu.com/a/998953669_121893286",
     "culture", "搜狐旅游"),
    ("武汉", "黄陂木兰山", "https://www.gushiwen.cn/GuShiWen_d075b40e82.aspx",
     "poem", "古诗文网"),
    ("恩施", "土家女儿城", "https://www.sohu.com/a/984308205_122311164",
     "culture", "搜狐旅游"),
    ("恩施", "土家女儿城", "https://baike.sogou.com/m/v9006.htm",
     "culture", "搜狗百科"),
    ("钟祥", "明显陵", "https://www.sohu.com/a/898806476_121893286",
     "culture", "搜狐旅游"),
]

NAV_WORDS = ["无障碍阅读", "长者模式", "手机版", "扫一扫", "当前页面", "上一篇",
             "下一篇", "版权所有", "技术支持", "联系我们", "网站地图", "返回顶部",
             "主办单位", "网站标识码", "备案号", "公安备案", "政府网站",
             "登录", "注册", "APP", "意见反馈", "收藏", "分享",
             # sohu/163 营销导航块
             "公开课", "严选", "VIP", "付费", "精品课程", "推荐收藏",
             "小九为您", "小九刚从", "关注我们", "点击跳转", "将跳转到第三方"]

CHUNK_MAX = 480
MAX_CHUNKS_PER_PAGE = 3
MIN_TEXT = 120  # 去噪后正文低于此值视为无效源


def _mk_soup(html):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    return soup


def _clean_segs(segs, title):
    seen, out = set(), []
    for s in segs:
        s = re.sub(r"\s+", " ", s).strip()
        if len(s) < 15:
            continue
        if s in seen:
            continue
        seen.add(s)
        if s == title:
            continue
        if any(w in s for w in NAV_WORDS) and len(s) < 60:
            continue
        out.append(s)
    return out


def extract_culture(html):
    soup = _mk_soup(html)
    h1 = soup.find(["h1", "h2", "title"])
    title = re.sub(r"\s+", " ", h1.get_text(" ", strip=True)).strip() if h1 else ""
    segs = []
    for tag in soup.find_all(["h1", "h2", "h3", "p", "li"]):
        txt = re.sub(r"\s+", " ", tag.get_text(" ", strip=True)).strip()
        if txt:
            segs.append(txt)
    return title, _clean_segs(segs, title)


def extract_poem(html):
    """古诗文网《木兰诗》页：原文在前（【译文】之前），其后为译文/注释。取原文作诗词正文。"""
    soup = _mk_soup(html)
    h1 = soup.find("h1")
    title = re.sub(r"\s+", " ", h1.get_text(" ", strip=True)).strip() if h1 else "木兰诗"
    full = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    # 取【译文】之前的原文部分
    m = re.search(r"【译文】|（译文）|译文[:：]", full)
    raw = full[:m.start()] if m else full
    # 去掉开头的站点导航噪声（"古诗文网 古文岛 ..." 之类）
    raw = re.sub(r"^.*?唧(?=[\(\（]?j|复唧唧|唧复)", "唧", raw, flags=re.S)
    raw = raw.strip()
    return title, raw


def chunk_text(text):
    if len(text) <= CHUNK_MAX:
        return [text] if text else []
    sents = re.split(r"(?<=[。！？；])", text)
    out, buf = [], ""
    for s in sents:
        if len(buf) + len(s) <= CHUNK_MAX:
            buf += s
        else:
            if buf:
                out.append(buf.strip())
            buf = s
    if buf.strip():
        out.append(buf.strip())
    return [c for c in out if len(c) >= 40]


def build_items(sess):
    items = []
    for city, spot, url, dtype, site in SOURCES:
        try:
            r = sess.get(url, timeout=20, verify=False, allow_redirects=True,
                         headers={"Referer": "https://www.sohu.com/"})
            r.encoding = r.apparent_encoding or "utf-8"
        except Exception as e:
            print(f"[ERR ] {spot}/{site} {type(e).__name__}: {str(e)[:70]}")
            continue

        if dtype == "poem":
            title, raw = extract_poem(r.text)
            if len(raw) < 60:
                print(f"[跳过] {spot}/{site} 诗词正文仅 {len(raw)} 字")
                continue
            items.append({"type": "poem", "title": title or "木兰诗",
                          "text": raw[:1500],
                          "metadata": {"city": city, "spot": spot,
                                       "source_url": url, "source_site": site,
                                       "origin": "web"}})
            print(f"[入库] {spot}/{site} 诗词 {len(raw)}字 -> 1条")
            continue

        title, segs = extract_culture(r.text)
        text = re.sub(r"\s+", " ", " ".join(segs)).strip()
        if len(text) < MIN_TEXT:
            print(f"[跳过] {spot}/{site} 去噪后仅 {len(text)} 字（源不可用）")
            continue
        chunks = chunk_text(text)[:MAX_CHUNKS_PER_PAGE]
        total = len(chunks)
        for i, ch in enumerate(chunks, 1):
            t = f"{spot}·{site}资料" if total == 1 else f"{spot}·{site}资料（{i}）"
            items.append({"type": dtype, "title": t, "text": ch,
                          "metadata": {"city": city, "spot": spot,
                                       "source_url": url, "source_site": site,
                                       "origin": "web"}})
        print(f"[入库] {spot}/{site} {len(segs)}段 -> {total}块")
    return items


def main():
    import urllib3
    urllib3.disable_warnings()

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA})

    items = build_items(sess)
    print(f"\n共生成 {len(items)} 条")

    kb = get_tourism_kb(asyncio.run(load_config()))
    print(f"TourismKB enabled={kb.enabled} emb={getattr(kb.embedding, 'type_key', '?')}")
    if not kb.enabled:
        print("!! 未启用，终止")
        return

    kb.clear("hubei_gap")
    n1 = kb.ingest_structured("hubei_gap", items)
    print(f"hubei_gap 新增 {n1} 条")

    docs = kb.list_docs("hubei_gap")
    merge = [{"type": d["type"], "title": d["title"], "text": d["text"],
              "metadata": dict(d.get("metadata") or {})} for d in docs]
    n2 = kb.ingest_structured("hubei", merge)
    print(f"合并进 hubei：{n2} 条（hubei 现有 {len(kb.list_docs('hubei'))} 条）")

    print("\n===== 抽查 =====")
    for q in ["神农架有什么传说", "荆州有什么典故", "襄阳古城的历史",
              "木兰诗讲的是什么", "土家族有什么习俗", "明显陵是谁的陵墓"]:
        print(f"问：{q}")
        for d in kb.hybrid_retrieve("hubei", q, top_k=3):
            print(f"   [{d.get('_sim', 0):.3f}] [{d.get('type')}] {d.get('title')}")


if __name__ == "__main__":
    main()
