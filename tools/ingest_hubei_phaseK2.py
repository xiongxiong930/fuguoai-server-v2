# -*- coding: utf-8 -*-
"""湖北文旅 Phase K2：补 2 条「360 百科 doc 深链」可达的文化/美食增量。

背景：Phase K 主批(ingest_hubei_phaseK.py)证实风俗/街角巷闻在 159 受限网下是硬上限
（百度403机房IP封/360搜索验证码/搜狐腾讯市级攻略=通用景点）。但 360 百科 doc 深链
直接抓取仍通——从被验证码的 360 搜索结果 HTML 里"顺带暴露"出 3 条深链，其中：
  - 恩施女儿会(doc/6185893-6399144)：与已入的「恩施土家族习俗与禁忌」重叠 → 跳过
  - 襄阳牛肉面(doc/4024971-4222448)：全新 food 增量
  - 咸宁桂花(doc/6904590-7126391)：全新 culture 增量
本批入这 2 条净增（无需代理、已实测 200+正文）。

用法：python3 tools/ingest_hubei_phaseK2.py  （容器内 /opt/fuguoai-server）
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

NAV = ["验证码","请输入验证码","正常访问","把您的IP","我们会尽快处理","首页","目录","相关词条",
       "猜你喜欢","免责声明","360百科","百科","义项指多义词","的不同概念",
       "中秋节的起源到底是什么","解决词条问题可获得额外积分","以更便捷易懂的方式获取知识",
       "咔咔小马多功能手机支架","词条","编辑","锁定","历史版本","贡献者","反馈","分享","收藏"]

# (url, title, type, city, spot, 校验词)
ITEMS = [
    ("https://baike.so.com/doc/4024971-4222448.html", "襄阳牛肉面", "food", "襄阳", "襄阳牛肉面", "襄阳"),
    ("https://baike.so.com/doc/6904590-7126391.html", "咸宁桂花", "culture", "咸宁", "咸宁桂花", "咸宁"),
]


def fetch_clean(url, kw, timeout=20):
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
            if l.strip() and not any(w in l for w in NAV) and len(l.strip()) >= 10]
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
                          "source_site": "360百科", "source_url": url, "origin": "web"},
        })
        print(f"[入库] {title}  {len(body)}字 (city={city}, type={dtype})")

    print(f"\n共生成 {len(items)} 条")
    if not items:
        print("无干净源可入，终止")
        return

    kb.clear("hubei_phaseK2")
    n1 = kb.ingest_structured("hubei_phaseK2", items)
    print(f"hubei_phaseK2 新增 {n1} 条")

    docs = kb.list_docs("hubei_phaseK2")
    merge = [{"type": d["type"], "title": d["title"], "text": d["text"],
              "metadata": dict(d.get("metadata") or {})} for d in docs]
    n2 = kb.ingest_structured("hubei", merge)
    print(f"合并进 hubei：+{n2} 条（hubei 现有 {len(kb.list_docs('hubei'))} 条）")

    print("\n===== 抽查（内存） =====")
    for q in ["襄阳有什么特色美食", "咸宁为什么叫桂花之城"]:
        print(f"问：{q}")
        for d in kb.hybrid_retrieve("hubei", q, top_k=3):
            print(f"   [{d.get('_sim', 0):.3f}|kw{d.get('_kw', 0):.2f}] [{d.get('type')}] {d.get('title')}")


if __name__ == "__main__":
    main()
