# -*- coding: utf-8 -*-
"""湖北文旅：公开网页抓取 → 去站点模板噪声 → 入库 hubei_web → 合并进默认 hubei 库。

用法（在 fuguoai-server 仓库根目录，或容器内 /opt/fuguoai-server）：
    python3 tools/ingest_hubei_web.py

设计要点（都是实测踩出来的，改前先读）：
1) config_holder.get_config() 在独立进程里是**空的**；且 load_config() 是**协程**，
   必须 asyncio.run(load_config()) 再传给 get_tourism_kb()，否则 enabled=False 静默不入库。
2) 站点导航/页脚在每页都出现（模板噪声）。若不去噪，所有文档因共享导航词互相误匹配。
   这里用两道过滤：同一 host 跨页重复段 + 通用导航词。
3) 有些站点（如武当山官网）所有页面 <title> 与 heading **完全相同**，只能靠 URL slug 区分
   ⇒ 标题由调用方显式给（"站点·栏目"），不要指望从页面自动推断。
4) 单页长文要限制切块数，否则一篇新闻独占知识库（首轮一篇新闻切出 13 块，占比过高）。
5) type 必须落在 tourism_kb._KB_TYPES 白名单 {scenic,food,route,stay,transport,festival,info,other}，
   否则 add_doc 会静默收敛成 other。判定顺序：先【标题+URL】再正文，否则正文里偶现的
   "交通"会把景区页误判成 transport。
6) 改完磁盘文件后**必须重启服务**：运行中的进程持有内存缓存，磁盘改了 HTTP 端点看不到。
"""
import asyncio
import os
import re
import sys
import urllib.parse
from collections import defaultdict

import requests
from bs4 import BeautifulSoup

# 定位仓库根：脚本可能放在仓库 tools/ 下，也可能被临时丢到 /tmp 在容器里跑，
# 因此先按 __file__ 推，推不出来再回退容器内路径（可用 FUGUOAI_ROOT 覆盖）。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not os.path.isdir(os.path.join(_ROOT, "fuguoai_core")):
    _ROOT = os.environ.get("FUGUOAI_ROOT", "/opt/fuguoai-server")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fuguoai_core.utils.tourism_kb import get_tourism_kb  # noqa: E402
from fuguoai_config.config_loader import load_config  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# (url, city, site, label)  —— label 由调用方给出，页面自身标题常常不可区分
PAGES = [
    ("https://www.chinawudang.com/scenic.html", "十堰", "武当山官网", "景点介绍"),
    ("https://www.chinawudang.com/strategy.html", "十堰", "武当山官网", "游玩攻略"),
    ("https://www.chinawudang.com/tiket.html", "十堰", "武当山官网", "门票政策"),
    ("http://www.esdaxiagu.com/scenic/", "恩施", "恩施大峡谷", "景区介绍"),
    ("http://www.esdaxiagu.com/scenicspot/", "恩施", "恩施大峡谷", "景点简介"),
    ("http://www.esdaxiagu.com/line/", "恩施", "恩施大峡谷", "线路推荐"),
    ("http://www.esdaxiagu.com/traffic/", "恩施", "恩施大峡谷", "交通指南"),
    ("https://wlt.hubei.gov.cn/bmdt/dtyw/202609/t20260909_6010494.shtml",
     "湖北", "湖北省文旅厅", "2026金秋消费季活动"),
]

NAV_WORDS = ["无障碍阅读", "长者模式", "手机版", "扫一扫", "当前页面", "上一篇",
             "下一篇", "版权所有", "技术支持", "联系我们", "网站地图", "返回顶部",
             "主办单位", "网站标识码", "备案号", "公安备案", "政府网站"]

CHUNK_MAX = 480
MAX_CHUNKS_PER_PAGE = 4


def fetch_segments(url, sess):
    r = sess.get(url, timeout=15, verify=False)
    r.encoding = r.apparent_encoding or "utf-8"
    soup = BeautifulSoup(r.text, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    segs = []
    for tag in soup.find_all(["h1", "h2", "h3", "p", "li"]):
        txt = re.sub(r"\s+", " ", tag.get_text(" ", strip=True)).strip()
        if len(txt) >= 10:
            segs.append(txt)
    return segs


def guess_type(label, url, text):
    """必须落在 _KB_TYPES 白名单内。先【标题+URL】再正文。"""

    def pick(s):
        if any(k in s for k in ["门票", "票价", "tiket", "ticket"]):
            return "info"
        if any(k in s for k in ["交通", "traffic", "怎么去"]):
            return "transport"
        if any(k in s for k in ["攻略", "线路", "行程", "游玩"]):
            return "route"
        if any(k in s for k in ["消费季", "节庆", "活动安排"]):
            return "festival"
        if any(k in s for k in ["景点", "景区", "scenic", "简介"]):
            return "scenic"
        return None

    return pick(label + " " + url) or pick(text[:200]) or "info"


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
    return [c for c in out if len(c) >= 60]


def build_items(sess):
    by_host = defaultdict(list)
    meta = {}
    for url, city, site, label in PAGES:
        host = urllib.parse.urlparse(url).netloc
        try:
            segs = fetch_segments(url, sess)
        except Exception as e:
            print(f"[ERR ] {url}  {type(e).__name__}: {str(e)[:70]}")
            continue
        print(f"[抓取] {site:<10}/{label:<12} segs={len(segs)}")
        by_host[host].append((url, segs))
        meta[url] = (city, site, label)

    boiler = {}
    for host, pages in by_host.items():
        cnt = defaultdict(int)
        for _, segs in pages:
            for s in set(segs):
                cnt[s] += 1
        n = len(pages)
        need = max(2, (n + 1) // 2)
        boiler[host] = {s for s, c in cnt.items() if c >= need and len(s) <= 40}
        print(f"[模板] {host:<22} 页={n} 重复段={len(boiler[host])}")

    items = []
    for host, pages in by_host.items():
        for url, segs in pages:
            city, site, label = meta[url]
            keep = [s for s in segs
                    if s not in boiler[host]
                    and not (len(s) <= 40 and any(w in s for w in NAV_WORDS))]
            text = re.sub(r"\s+", " ", " ".join(keep)).strip()
            if len(text) < 150:
                print(f"[跳过] {site}/{label} 去噪后仅 {len(text)} 字")
                continue
            chunks = chunk_text(text)[:MAX_CHUNKS_PER_PAGE]
            total = len(chunks)
            for i, ch in enumerate(chunks, 1):
                title = f"{site}·{label}" if total == 1 else f"{site}·{label}（{i}）"
                items.append({
                    "type": guess_type(label, url, ch),
                    "title": title,
                    "text": ch,
                    "metadata": {"city": city, "source_url": url,
                                 "source_site": host, "origin": "web"},
                })
            print(f"[入库] {site}/{label} {len(segs)}->{len(keep)}段 {len(text)}字 -> {total}块")
    return items


def main():
    import urllib3
    urllib3.disable_warnings()

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA})
    items = build_items(sess)
    print(f"\n共生成 {len(items)} 条\n")

    kb = get_tourism_kb(asyncio.run(load_config()))
    print(f"TourismKB enabled={kb.enabled} emb={getattr(kb.embedding, 'type_key', '?')}")
    if not kb.enabled:
        print("!! 未启用，终止")
        return

    kb.clear("hubei_web")
    n1 = kb.ingest_structured("hubei_web", items)
    print(f"hubei_web 新增 {n1} 条")

    web = kb.list_docs("hubei_web")
    merge = [{"type": d["type"], "title": d["title"], "text": d["text"],
              "metadata": dict(d.get("metadata") or {})} for d in web]
    n2 = kb.ingest_structured("hubei", merge)
    print(f"合并进 hubei：{n2} 条（hubei 现有 {len(kb.list_docs('hubei'))} 条）")

    print("\n===== 抽查 =====")
    for q in ["武当山门票多少钱", "恩施大峡谷怎么去", "恩施大峡谷有哪些景点"]:
        print(f"问：{q}")
        for d in kb.hybrid_retrieve("hubei", q, top_k=3):
            print(f"   [{d.get('_sim', 0):.3f}|kw{d.get('_kw', 0):.2f}] "
                  f"[{d.get('type')}|{d.get('metadata', {}).get('origin', 'seed')}] {d.get('title')}")


if __name__ == "__main__":
    main()
