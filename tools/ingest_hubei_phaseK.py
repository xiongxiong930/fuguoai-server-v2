# -*- coding: utf-8 -*-
"""湖北文旅 Phase K：每城「风俗习惯 + 街角巷闻」补数据。

⚠️ 实证硬上限（4 轮探针 phaseK1~4，159 容器出网）：
  百度百科 403 反爬(拦截页非正文) / 知乎专栏 404 / gov 首页乱码墙 /
  360百科 bing 不排深链(site: 强制仍回百度) / 搜狐·腾讯「市级攻略」排上来的是
  通用景点攻略(十大景区·每天了解一个城市)而非风俗 / 腾讯新闻多回百度或视频页。
  ⇒ 风俗/街角巷闻类在本网络下无干净专属源，与 Phase J urban POI 同属硬上限。
     唯一干净正题源 = 恩施土家族习俗与禁忌（搜狐，2132字，质量高）。

本批只入【实测干净·正题】的 1 篇（恩施），不抓景点攻略误标为风俗：
  1. 搜狐 恩施土家族习俗与禁忌 -> culture(习俗与禁忌：拦门酒/跳丧舞/女儿会/咂酒/火塘禁忌/社节)

用法：python3 tools/ingest_hubei_phaseK.py  （容器内 /opt/fuguoai-server）
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
       "良医","地图","文库","软件","翻译","搜索","纠错","目录","简介","历史沿革","基本概况",
       "相关词条","猜你喜欢","热门词条","免责声明","版权",
       # 搜狐号 promo 噪声
       "返回搜狐","平台声明","搜狐号系信息发布平台","搜狐仅提供信息存储空间服务","该文观点仅代表作者本人",
       "原创整理","查看TA的文章","文章","总阅读","阅读","责任编辑","本文由","湖北荆钰",
       "信息发布平台","搜狐号"]

# (url, title, type, city, spot, 校验词)
ITEMS = [
    ("https://www.sohu.com/a/984308205_122311164", "恩施土家族习俗与禁忌", "culture",
     "恩施", "土家习俗", "土家族"),
]


def fetch_clean(url, kw, timeout=25):
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
    if kw not in body:
        print(f"  [跳过] {url}：正文不含'{kw}'（可能错源/空页）")
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
                          "source_site": "搜狐", "source_url": url, "origin": "web"},
        })
        print(f"[入库] {title}  {len(body)}字 (city={city}, type={dtype})")

    print(f"\n共生成 {len(items)} 条")
    if not items:
        print("无干净源可入，终止（风俗/街角巷闻为 159 出网硬上限）")
        return

    kb.clear("hubei_phaseK")
    n1 = kb.ingest_structured("hubei_phaseK", items)
    print(f"hubei_phaseK 新增 {n1} 条")

    docs = kb.list_docs("hubei_phaseK")
    merge = [{"type": d["type"], "title": d["title"], "text": d["text"],
              "metadata": dict(d.get("metadata") or {})} for d in docs]
    n2 = kb.ingest_structured("hubei", merge)
    print(f"合并进 hubei：+{n2} 条（hubei 现有 {len(kb.list_docs('hubei'))} 条）")

    print("\n===== 抽查（内存） =====")
    for q in ["恩施土家族有什么特别的习俗", "土家族的女儿会是什么", "土家族有哪些禁忌"]:
        print(f"问：{q}")
        for d in kb.hybrid_retrieve("hubei", q, top_k=3):
            print(f"   [{d.get('_sim', 0):.3f}|kw{d.get('_kw', 0):.2f}] [{d.get('type')}] {d.get('title')}")


if __name__ == "__main__":
    main()
