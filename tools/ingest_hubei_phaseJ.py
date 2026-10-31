# -*- coding: utf-8 -*-
"""湖北文旅 Phase J：城市 urban POI（商业街/广场/公园/网红街巷）补数据。

⚠️ 实证硬上限（4 轮探针，159 容器出网）：
  知乎专栏 404 / 百度百科 403 / gov 首页乱码 / bing 误匹配无关域(哈尔滨理工·昆明政府·Minitab·黎姿) /
  马蜂窝 captcha 拦截 / 360百科仅「江汉路·武汉步行街」有对城干净词条 / 搜狐攻略 bing 未返回。
  ⇒ 仅武汉有 3 篇干净可达源，其余城市 urban POI 在本网络下无干净源，等更好源再补（同晴川阁/长江大桥硬上限）。

本批只入【实测干净】的 3 篇（均武汉），不抓垃圾：
  1. 360百科 武汉江汉路  -> scenic(商业街)
  2. 360百科 武汉步行街 -> scenic(商业街)
  3. 腾讯新闻 汉口与武汉三镇由来 -> culture(街角巷闻)

用法：python3 tools/ingest_hubei_phaseJ.py  （容器内 /opt/fuguoai-server）
改完磁盘后必须重启服务（进程持有内存缓存），HTTP 端点才看得到。
"""
import asyncio
import os
import re
import sys

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings()

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not os.path.isdir(os.path.join(_ROOT, "fuguoai_core")):
    _ROOT = os.environ.get("FUGUOAI_ROOT", "/opt/fuguoai-server")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fuguoai_core.utils.tourism_kb import get_tourism_kb  # noqa: E402
from fuguoai_config.config_loader import load_config  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

NAV = ["公开课","严选","VIP","付费","精品课程","推荐收藏","小九为您","关注我们","点击跳转",
       "将跳转到第三方","登录","注册","下载APP","客户端","广告","首页","频道","展开全文","收起",
       "分享","收藏","进入词条","搜索词条","反馈","消息","网页","资讯","AI问答","视频","图片",
       "良医","地图","文库","软件","翻译","包头","搜索","纠错","收藏","目录","简介","历史沿革",
       "基本概况","相关词条","猜你喜欢","热门词条","免责声明","版权"]

# (url, title, type, city, spot, 校验城市词)
ITEMS = [
    ("https://baike.so.com/doc/6803170-7020059.html", "江汉路步行街", "scenic", "武汉", "江汉路步行街", "武汉"),
    ("https://baike.so.com/doc/5869726-6082585.html", "武汉步行街", "scenic", "武汉", "武汉步行街", "武汉"),
    ("https://news.qq.com/rain/a/20230417A07FC400", "汉口与武汉三镇（街巷由来）", "culture", "武汉", "汉口江滩", "汉口"),
]


def fetch_clean(url, city_kw, timeout=20):
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout, verify=False)
    except Exception as e:
        print(f"  [ERR ] {url} {type(e).__name__}: {str(e)[:60]}")
        return None
    if not r.text:
        return None
    s = BeautifulSoup(r.text, "html.parser")
    for t in s(["script", "style", "nav", "header", "footer", "aside", "noscript", "iframe"]):
        t.decompose()
    txt = s.get_text("\n")
    segs = [l.strip() for l in txt.splitlines()
            if l.strip() and not any(w in l for w in NAV) and len(l.strip()) >= 8]
    body = re.sub(r"\s+", "", " ".join(segs))
    if city_kw not in body:
        print(f"  [跳过] {url}：正文不含'{city_kw}'（可能错城/空页）")
        return None
    if len(body) < 200:
        print(f"  [跳过] {url}：正文过短({len(body)}字)")
        return None
    return body


def main():
    kb = get_tourism_kb(asyncio.run(load_config()))
    print(f"TourismKB enabled={kb.enabled} emb={getattr(kb.embedding, 'type_key', '?')}")
    if not kb.enabled:
        print("!! 未启用，终止")
        return

    items = []
    for url, title, dtype, city, spot, kw in ITEMS:
        body = fetch_clean(url, kw)
        if not body:
            continue
        items.append({
            "type": dtype, "title": title, "text": body,
            "metadata": {"city": city, "spot": spot,
                          "source_site": "360百科/腾讯新闻", "source_url": url, "origin": "web"},
        })
        print(f"[入库] {title}  {len(body)}字 (city={city}, type={dtype})")

    print(f"\n共生成 {len(items)} 条")
    if not items:
        print("无干净源可入，终止（其余城市 urban POI 为 159 出网硬上限）")
        return

    kb.clear("hubei_phaseJ")
    n1 = kb.ingest_structured("hubei_phaseJ", items)
    print(f"hubei_phaseJ 新增 {n1} 条")

    docs = kb.list_docs("hubei_phaseJ")
    merge = [{"type": d["type"], "title": d["title"], "text": d["text"],
              "metadata": dict(d.get("metadata") or {})} for d in docs]
    n2 = kb.ingest_structured("hubei", merge)
    print(f"合并进 hubei：+{n2} 条（hubei 现有 {len(kb.list_docs('hubei'))} 条）")

    print("\n===== 抽查（内存） =====")
    for q in ["武汉有哪些有名的商业街", "江汉路步行街是什么", "汉口和武汉三镇有什么关系"]:
        print(f"问：{q}")
        for d in kb.hybrid_retrieve("hubei", q, top_k=3):
            print(f"   [{d.get('_sim', 0):.3f}|kw{d.get('_kw', 0):.2f}] [{d.get('type')}] {d.get('title')}")


if __name__ == "__main__":
    main()
