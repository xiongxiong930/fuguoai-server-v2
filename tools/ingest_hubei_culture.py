# -*- coding: utf-8 -*-
"""湖北文旅：逸闻故事 / 文化典故 / 诗词 素材抓取入库。

两路数据源（都是 159 实测可达的公开页面）：
  A. 武当山官网文化栏目（武当游记/太和山志/建筑文化/道家文化/神仙文化）→ type=culture
  B. 古诗文网（古文岛）按【标签】列出诗文 → 逐篇抓正文 → type=poem
     入口规律：https://www.gushiwen.cn/shiwens/default.aspx?tstr=<标签>
     注意：它的 search.aspx 与作者页都拿不到列表（JS/空壳），只有 tstr 标签列表页可用。

用法（仓库根目录 或 容器内 /opt/fuguoai-server）：
    python3 tools/ingest_hubei_culture.py

注意：type 必须落在 tourism_kb._KB_TYPES 白名单；culture/poem 是 2026-09-23 新增的。
改完磁盘后必须重启服务，HTTP 端点才看得到（进程持有内存缓存）。
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

# ---------- A. 武当山文化栏目（url, city, site, label, type） ----------
WUDANG_PAGES = [
    ("https://www.chinawudang.com/travelogue.html", "十堰", "武当山", "武当游记", "culture"),
    ("https://www.chinawudang.com/records.html", "十堰", "武当山", "太和山志", "culture"),
    ("https://www.chinawudang.com/culture.html", "十堰", "武当山", "建筑文化", "culture"),
    ("https://www.chinawudang.com/taoist.html", "十堰", "武当山", "道家文化", "culture"),
    ("https://www.chinawudang.com/taoistdeities.html", "十堰", "武当山", "神仙文化", "culture"),
    # 「武当八百里」是逐集讲具体景点典故的系列（紫金城/隐士岩/南岩/琼台三观/状元岩…），
    # 正是"各景点逸闻故事"的富矿；列表页 + 若干集详情页一起抓。
    ("https://www.chinawudang.com/eighthundredmiles.html", "十堰", "武当山", "武当八百里", "culture"),
    ("https://www.chinawudang.com/eighthundredmiles/7918.html", "十堰", "武当山", "八百里·紫金城", "culture"),
    ("https://www.chinawudang.com/eighthundredmiles/7917.html", "十堰", "武当山", "八百里·隐士岩", "culture"),
    ("https://www.chinawudang.com/eighthundredmiles/7916.html", "十堰", "武当山", "八百里·南岩", "culture"),
    ("https://www.chinawudang.com/eighthundredmiles/7915.html", "十堰", "武当山", "八百里·琼台三观", "culture"),
    ("https://www.chinawudang.com/eighthundredmiles/7914.html", "十堰", "武当山", "八百里·状元岩", "culture"),
    ("https://www.chinawudang.com/eighthundredmiles/7913.html", "十堰", "武当山", "八百里·华阳岩", "culture"),
    ("https://www.chinawudang.com/stone.html", "十堰", "武当山", "石刻碑铭", "culture"),
]

# 古诗文网的标签体系很稀疏：赤壁/三峡/武汉/武当山等标签命中 0，
# 实测只有「黄鹤楼 / 怀古 / 长江 / 知音」有内容。因此用【标签 + 标题关键词】双重筛选：
# 先取标签列表，再只保留标题含景点关键词的诗（如「怀古」标签里筛出《念奴娇·赤壁怀古》），
# 避免把《滕王阁序》这类非湖北篇目混进来。
POEM_TAGS = [
    ("黄鹤楼", "武汉", "黄鹤楼", ["黄鹤楼"]),
    ("怀古", "黄冈", "赤壁", ["赤壁"]),
    ("长江", "宜昌", "长江三峡", ["三峡", "白帝", "荆门"]),
    ("知音", "武汉", "古琴台", ["知音", "伯牙"]),
    # 屈原/楚辞：补东湖·行吟阁（屈原）的诗词缺口；注意荆州/襄阳/诸葛亮/巫山等标签命中 0。
    ("屈原", "武汉", "东湖·行吟阁", ["离骚", "九歌"]),
    ("楚", "武汉", "东湖·行吟阁", ["渔父"]),
]
POEMS_PER_TAG = 3
POEM_TEXT_MAX = 1200

NAV_WORDS = ["无障碍阅读", "长者模式", "手机版", "扫一扫", "当前页面", "上一篇",
             "下一篇", "版权所有", "技术支持", "联系我们", "网站地图", "返回顶部",
             "主办单位", "网站标识码", "备案号", "公安备案", "政府网站",
             "登录", "注册", "APP", "意见反馈", "收藏", "分享"]

CHUNK_MAX = 480
MAX_CHUNKS_PER_PAGE = 3
# 跨页重复段的字符上限。太小滤不掉"相关阅读"整段（约 120~180 字），
# 太大（实测 600）会误杀正文（「建筑文化」整页被判为共享块而整页丢失）。200 是实测平衡点。
BOILER_MAX_LEN = 200


def _mk_soup(html):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    return soup


def extract_prose(html):
    """散文/故事页：取 h1-h3/p/li。"""
    soup = _mk_soup(html)
    h1 = soup.find(["h1", "h2"])
    title = re.sub(r"\s+", " ", h1.get_text(" ", strip=True)) if h1 else ""
    segs = []
    for tag in soup.find_all(["h1", "h2", "h3", "p", "li"]):
        txt = re.sub(r"\s+", " ", tag.get_text(" ", strip=True)).strip()
        if len(txt) >= 10:
            segs.append(txt)
    return title, segs


def extract_poem(html):
    """诗词页：古诗文网正文常在 div 里，需带上 div；并优先取 <h1> 作标题。"""
    soup = _mk_soup(html)
    h1 = soup.find("h1")
    title = re.sub(r"\s+", " ", h1.get_text(" ", strip=True)) if h1 else ""
    segs, seen = [], set()
    for tag in soup.find_all(["h1", "h2", "h3", "p", "div"]):
        txt = re.sub(r"\s+", " ", tag.get_text(" ", strip=True)).strip()
        if len(txt) >= 10 and txt not in seen:
            seen.add(txt)
            segs.append(txt)
    return title, segs


def strip_boiler(segs, boiler):
    keep = [s for s in segs
            if s not in boiler
            and not (len(s) <= 40 and any(w in s for w in NAV_WORDS))]
    return keep


SERIES_RE = re.compile(r"《武当八百里》第")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def cut_related(text, own_label):
    """截断分集页底部的「相关阅读」区块。

    注意：标题与日期被拆成了不同标签段（<a>标题</a> + <span>日期</span>），
    逐段匹配「系列名+日期戳」会落空，必须在**合并后的文本**上按边界截断：
    从首个日期戳（正文起点）之后，找下一个《武当八百里》第N集 且不含本集名的位置，切掉。
    """
    m = DATE_RE.search(text)
    pos = m.end() if m else 0
    idx = text.find("《武当八百里》第", pos)
    while idx != -1:
        if own_label not in text[idx:idx + 60]:
            return text[:idx]
        idx = text.find("《武当八百里》第", idx + 1)
    return text


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


def build_wudang(sess):
    by_host = defaultdict(list)
    meta = {}
    for url, city, site, label, dtype in WUDANG_PAGES:
        host = urllib.parse.urlparse(url).netloc
        try:
            r = sess.get(url, timeout=15, verify=False)
            r.encoding = r.apparent_encoding or "utf-8"
            _, segs = extract_prose(r.text)
        except Exception as e:
            print(f"[ERR ] {url} {type(e).__name__}: {str(e)[:70]}")
            continue
        print(f"[抓取] 武当山/{label} segs={len(segs)}")
        by_host[host].append((url, segs))
        meta[url] = (city, site, label, dtype)

    boiler = {}
    for host, pages in by_host.items():
        cnt = defaultdict(int)
        for _, segs in pages:
            for s in set(segs):
                cnt[s] += 1
        # 长度上限必须放宽：「相关阅读/推荐」在每集详情页底部整段重复（约 400+ 字），
        # 若沿用短句上限(40)就滤不掉，导致各集的第 3 块完全相同、检索时互相误命中。
        n = len(pages)
        boiler[host] = {s for s, c in cnt.items()
                        if c >= max(2, (n + 1) // 2) and len(s) <= BOILER_MAX_LEN}
        print(f"[模板] {host} 页={n} 重复段={len(boiler[host])}")

    items = []
    for host, pages in by_host.items():
        for url, segs in pages:
            city, site, label, dtype = meta[url]
            keep = strip_boiler(segs, boiler[host])
            text = re.sub(r"\s+", " ", " ".join(keep)).strip()
            if label.startswith("八百里·"):
                own = label.split("·", 1)[1]
                cut = cut_related(text, own)
                if len(cut) != len(text):
                    print(f"[去相关] {site}/{label} {len(text)}->{len(cut)} 字")
                text = cut
            if len(text) < 150:
                print(f"[跳过] {site}/{label} 去噪后仅 {len(text)} 字")
                continue
            chunks = chunk_text(text)[:MAX_CHUNKS_PER_PAGE]
            total = len(chunks)
            for i, ch in enumerate(chunks, 1):
                title = f"{site}·{label}" if total == 1 else f"{site}·{label}（{i}）"
                items.append({"type": dtype, "title": title, "text": ch,
                              "metadata": {"city": city, "source_url": url,
                                           "source_site": host, "origin": "web"}})
            print(f"[入库] {site}/{label} {len(segs)}->{len(keep)}段 -> {total}块")
    return items


def build_poems(sess):
    """古诗文网：标签列表页 -> 标题关键词筛选 -> 逐篇抓正文 -> 跨页去噪。

    关键：诗词页顶部有约 120 字的音频播放器/导航块（"0.25x 列表循环 您的浏览器不支持
    audio 元素"），且每页都一样；不去掉会让所有诗词条目互相误匹配。这里把所有诗词页
    一起算跨页重复段（长度上限放宽到 200，因为播放器块是一整段而非短句）。
    """
    cands = []
    for tag, city, spot, kws in POEM_TAGS:
        list_url = ("https://www.gushiwen.cn/shiwens/default.aspx?tstr="
                    + urllib.parse.quote(tag))
        try:
            r = sess.get(list_url, timeout=15, verify=False)
            r.encoding = r.apparent_encoding or "utf-8"
            soup = BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            print(f"[ERR ] 列表 {tag} {type(e).__name__}: {str(e)[:70]}")
            continue
        seen, matched = set(), []
        for a in soup.find_all("a", href=True):
            if "shiwenv" not in a["href"]:
                continue
            href = urllib.parse.urljoin(list_url, a["href"]).split("#")[0]
            txt = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
            if not txt or href in seen:
                continue
            seen.add(href)
            if any(k in txt for k in kws):
                matched.append((txt, href))
        if not matched:
            print(f"[诗词] 标签「{tag}」无标题含 {kws} 的篇目，跳过")
            continue
        print(f"[诗词] 标签「{tag}」匹配 {len(matched)} 篇，取 {min(POEMS_PER_TAG, len(matched))}")
        for txt, href in matched[:POEMS_PER_TAG]:
            cands.append((spot, city, txt, href))

    pages = []
    for spot, city, atitle, url in cands:
        try:
            rr = sess.get(url, timeout=15, verify=False)
            rr.encoding = rr.apparent_encoding or "utf-8"
            title, segs = extract_poem(rr.text)
        except Exception as e:
            print(f"[ERR ] {url} {type(e).__name__}: {str(e)[:60]}")
            continue
        pages.append({"spot": spot, "city": city, "atitle": atitle,
                      "url": url, "title": title, "segs": segs})

    if not pages:
        return []

    cnt = defaultdict(int)
    for p in pages:
        for s in set(p["segs"]):
            cnt[s] += 1
    n = len(pages)
    need = max(2, (n + 1) // 2)
    boiler = {s for s, c in cnt.items() if c >= need and len(s) <= 200}
    print(f"[模板] gushiwen 页={n} 重复段={len(boiler)}")

    items = []
    for p in pages:
        keep = strip_boiler(p["segs"], boiler)
        text = re.sub(r"https?://\S+", "", " ".join(keep))
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < 60:
            print(f"[跳过] {p['atitle']} 去噪后仅 {len(text)} 字")
            continue
        t = (p["title"] or p["atitle"]).strip()[:40]
        full = t if p["spot"] in t else f"{t}（{p['spot']}）"
        items.append({"type": "poem", "title": full, "text": text[:POEM_TEXT_MAX],
                      "metadata": {"city": p["city"], "spot": p["spot"],
                                   "source_url": p["url"],
                                   "source_site": "www.gushiwen.cn",
                                   "origin": "web"}})
        print(f"   [入库] {full[:40]}  {min(len(text), POEM_TEXT_MAX)}字")
    return items


def main():
    import urllib3
    urllib3.disable_warnings()

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA})

    items = build_wudang(sess)
    print()
    items += build_poems(sess)
    print(f"\n共生成 {len(items)} 条")

    kb = get_tourism_kb(asyncio.run(load_config()))
    print(f"TourismKB enabled={kb.enabled} emb={getattr(kb.embedding, 'type_key', '?')}")
    if not kb.enabled:
        print("!! 未启用，终止")
        return

    kb.clear("hubei_culture")
    n1 = kb.ingest_structured("hubei_culture", items)
    print(f"hubei_culture 新增 {n1} 条")

    docs = kb.list_docs("hubei_culture")
    merge = [{"type": d["type"], "title": d["title"], "text": d["text"],
              "metadata": dict(d.get("metadata") or {})} for d in docs]
    n2 = kb.ingest_structured("hubei", merge)
    print(f"合并进 hubei：{n2} 条（hubei 现有 {len(kb.list_docs('hubei'))} 条）")

    print("\n===== 抽查 =====")
    for q in ["黄鹤楼有什么诗词", "武当山有什么传说故事", "赤壁的诗词",
              "崔颢写的黄鹤楼", "知音的故事"]:
        print(f"问：{q}")
        for d in kb.hybrid_retrieve("hubei", q, top_k=3):
            print(f"   [{d.get('_sim', 0):.3f}|kw{d.get('_kw', 0):.2f}] "
                  f"[{d.get('type')}] {d.get('title')}")


if __name__ == "__main__":
    main()
