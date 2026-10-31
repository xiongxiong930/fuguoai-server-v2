# -*- coding: utf-8 -*-
"""湖北文旅 Phase I：补「更多相关景点的古文/赋」入库（苏轼前后赤壁赋、郦道元三峡）。

数据源（均为 159 容器实测可达、且内容干净的公开页）：
  - 百度汉语 hanyu.baidu.com 搜索结果页：对「前赤壁赋 / 后赤壁赋 / 三峡」可干净抽出古文原文，
    无 403（百度百科 baike.baidu.com 是 403 反爬，不可用），无 JS 空壳。
    取正文锚点 + 大窗口 + 去 HTML 标签即可。

为什么选这 3 篇（不重复、贴合"景点故事/典故"）：
  - Phase G 的 POEM_TAGS 已覆盖 黄鹤楼/赤壁怀古(念奴娇)/长江三峡诗词/知音/屈原 诗词，
    故「三峡诗词 / 赤壁诗词」不再重复加；这里加的是【古文/赋】原文（不同 doc 类型语义）。
  - 东坡赤壁（黄州）的立身之本就是苏轼《前后赤壁赋》；长江三峡的顶级文学注脚是郦道元《三峡》。
  - 跳过项（实测硬上限/源脏）：长江大桥（政府站+旅游软文，无干净专文）、三峡大坝（知乎专栏404/
    搜狗百科JS壳/ctg官网导航）、归元寺（官方寺站 list.php 只有导航无正文、知乎问题404、微信文已注销）。

用法（仓库根 或 容器内 /opt/fuguoai-server）：
    python3 tools/ingest_hubei_more.py
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

# (查询词, 正文锚点, 标题, 城市, 景点, 简介)
HANYU_ITEMS = [
    ("三峡", "自三峡七百里中", "郦道元《三峡》（《水经注·江水》）", "宜昌",
     "长江三峡", "北魏郦道元《水经注》名篇，写三峡七百里山势、夏水襄陵之迅疾、春冬清荣峻茂、晴初霜旦高猿长啸。"),
    ("前赤壁赋", "壬戌之秋", "苏轼《前赤壁赋》", "黄冈",
     "东坡赤壁", "北宋苏轼谪居黄州时夜游赤壁之作，清风明月、沧海一粟、物我两忘，东坡赤壁（黄州）的文学根基。"),
    ("后赤壁赋", "是岁十月之望", "苏轼《后赤壁赋》", "黄冈",
     "东坡赤壁", "苏轼《前赤壁赋》姊妹篇，十月再游赤壁，履巉岩、踞虎豹、登虬龙，末以孤鹤道士梦结，意境幽奇。"),
]

NAV_WORDS = ["公开课", "严选", "VIP", "付费", "精品课程", "推荐收藏", "小九为您", "关注我们",
             "点击跳转", "将跳转到第三方", "登录", "注册", "下载APP", "客户端", "广告",
             "首页", "频道", "展开全文", "收起", "分享", "收藏", "您的浏览器不支持"]


def fetch_hanyu(query, anchor, timeout=20):
    url = "https://hanyu.baidu.com/s?wd=" + requests.utils.quote(query)
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout, verify=False)
    except Exception as e:
        print(f"  [ERR ] hanyu {query} {type(e).__name__}: {str(e)[:60]}")
        return None
    if not r.text:
        return None
    # 锚点法：从古文开头抓一大段（覆盖整篇赋/短文）。窗口放大到 3000，
    # 因为 hanyu 会把「注释弹层」<span class='body-zhushi-span' data='…长json…'>
    # 嵌在正文里，需连同其 data 一起裁掉才能露出真正的古文。
    m = re.search(re.escape(anchor) + r".{0,3000}", r.text)
    if not m:
        return None
    raw = m.group(0)
    # 先去注释弹层 span（data 属性可能很长，先按 <span...> 整体删），再去其余标签
    raw = re.sub(r"<span[^>]*>", "", raw)
    raw = re.sub(r"<[^>]+>", "", raw)
    raw = re.sub(r"&[a-z]+;", "", raw)
    raw = re.sub(r"\s+", "", raw)
    return raw


def clean_for_kb(text, intro):
    """拼成可读条目：古文原文 + 简介。"""
    if not text:
        return None
    # 只保留中文与常见标点，去掉残留英文/url
    body = re.sub(r"[A-Za-z]{2,}", "", text)
    body = re.sub(r"https?://\S+", "", body)
    body = body.strip(" 。，、；：？！")
    if len(body) < 60:
        return None
    return f"{body}\n\n【简介】{intro}"


def build_items():
    items = []
    for query, anchor, title, city, spot, intro in HANYU_ITEMS:
        raw = fetch_hanyu(query, anchor)
        if not raw:
            print(f"[跳过] {title}：hanyu 未命中锚点")
            continue
        text = clean_for_kb(raw, intro)
        if not text:
            print(f"[跳过] {title}：清洗后过短")
            continue
        items.append({
            "type": "culture",
            "title": title,
            "text": text,
            "metadata": {
                "city": city,
                "spot": spot,
                "source_site": "hanyu.baidu.com",
                "source_url": "https://hanyu.baidu.com/s?wd=" + requests.utils.quote(query),
                "origin": "web",
            },
        })
        print(f"[入库] {title}  {len(text)}字 (city={city}, spot={spot})")
    return items


def main():
    kb = get_tourism_kb(asyncio.run(load_config()))
    print(f"TourismKB enabled={kb.enabled} emb={getattr(kb.embedding, 'type_key', '?')}")
    if not kb.enabled:
        print("!! 未启用，终止")
        return

    items = build_items()
    print(f"\n共生成 {len(items)} 条")

    kb.clear("hubei_more")
    n1 = kb.ingest_structured("hubei_more", items)
    print(f"hubei_more 新增 {n1} 条")

    docs = kb.list_docs("hubei_more")
    merge = [{"type": d["type"], "title": d["title"], "text": d["text"],
              "metadata": dict(d.get("metadata") or {})} for d in docs]
    n2 = kb.ingest_structured("hubei", merge)
    before = len(kb.list_docs("hubei")) - n2
    print(f"合并进 hubei：+{n2} 条（hubei 现有 {len(kb.list_docs('hubei'))} 条，本次前 {before}）")

    print("\n===== 抽查（内存） =====")
    for q in ["东坡赤壁有什么典故", "苏轼在黄州写了什么", "三峡的古文出处",
              "郦道元写了三峡什么", "前后赤壁赋"]:
        print(f"问：{q}")
        for d in kb.hybrid_retrieve("hubei", q, top_k=3):
            print(f"   [{d.get('_sim', 0):.3f}|kw{d.get('_kw', 0):.2f}] "
                  f"[{d.get('type')}] {d.get('title')}")


if __name__ == "__main__":
    main()
