# -*- coding: utf-8 -*-
"""湖北文旅 KB 抓取噪声清理（通用、纯删除、不编造）。

背景：2026-09-26 全库扫描发现多条条目正文是**百科页面抓取残留**——开头带
  ① 360百科/百科页头（"……_360百科……义项指多义词……查看详细规范>>"）
  ② 抓取时间戳（"2023-02-0719:30:48"）
  ③ 乱码"义项"说明（"X的义项：网球运动员、歌手等；Y的义项：冯小刚执导电影、江苏卫视交友节目等"）
  结尾带引用残渣（"[2014-5-12].中国桂花网……"）。
这些噪声会污染检索与 LLM 答案（如"襄阳牛肉面"条目曾整段不可用）。

原则：**只删噪声、绝不加内容**。中间的真实正文（如江汉路全长1600米、咸宁屈原咏桂诗句等）
  原样保留 ⇒ 不引入任何未经核实的事实。

做法：命中噪声的条目，按标题精确摘除旧条目 → 用清洗后正文重新 embed 入库
  （与本仓 tools/tourism_kb_update_docs.py 同一机制：add_doc 的 id 依赖正文，必须删+重录）。

跑法（容器内 /opt/fuguoai-server，embedding 缓存只在容器有）：
  python3 tools/tourism_kb_clean_noise.py            # 实际执行
  python3 tools/tourism_kb_clean_noise.py --dry-run  # 只看会改什么，不落盘
"""
import asyncio
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not os.path.isdir(os.path.join(_ROOT, "fuguoai_core")):
    _ROOT = os.environ.get("FUGUOAI_ROOT", "/opt/fuguoai-server")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fuguoai_config.config_loader import load_config  # noqa: E402
from fuguoai_core.utils.tourism_kb import get_tourism_kb  # noqa: E402

KB_ID = "hubei"
MIN_KEEP = 80  # 清洗后正文至少保留这么多字，否则视为异常、跳过不动

# ---- 开头噪声（按序反复剥离，直到不再变化）----
HEAD_PATTERNS = [
    # 360百科/百科页头：从开头一直到 "查看详细规范>>"
    re.compile(r'^.*?查看详细规范\s*>>', re.S),
    # 抓取时间戳开头
    re.compile(r'^\s*\d{4}-\d{2}-\d{2}\s*\d{2}:\d{2}:\d{2}'),
    # 乱码"义项"说明链：X的义项：……等；
    re.compile(r'^(?:\s*[^，。；\s]{0,12}的义项：[^。；]*?等[；。]?)+'),
]
# ---- 结尾噪声 ----
TAIL_PATTERNS = [
    # 引用残渣：[2014-5-12] 及其后
    re.compile(r'\[\d{4}-\d{1,2}-\d{1,2}\][^。]*$', re.S),
    # 结尾 "…网站.[2014…" 之类
    re.compile(r'(?:网站|百科|来源)\.?\[?\s*\d{4}.*$', re.S),
]


def clean_text(t):
    """只删除首尾噪声，中间原样保留。"""
    s = t or ""
    changed = True
    while changed:
        changed = False
        for rx in HEAD_PATTERNS:
            new = rx.sub('', s, count=1)
            if new != s:
                s = new.lstrip()
                changed = True
    for rx in TAIL_PATTERNS:
        s = rx.sub('', s, count=1)
    return s.strip()


def main():
    dry = '--dry-run' in sys.argv
    config = asyncio.run(load_config())
    from fuguoai_core.utils.config_holder import set_config
    set_config(config)
    kb = get_tourism_kb()
    if kb is None or not kb.enabled:
        print("ERROR: tourism kb 未启用或单例为空")
        sys.exit(2)

    data = kb._get_mem(KB_ID)
    docs = data.setdefault("docs", [])
    before_total = len(docs)
    print(f"清理前 {KB_ID} 条数 = {before_total}{'（DRY-RUN，不落盘）' if dry else ''}")

    targets = []
    for d in docs:
        t = d.get("text") or ""
        c = clean_text(t)
        if c != t.strip() and len(c) >= MIN_KEEP:
            targets.append((d, c))

    print(f"命中噪声条目 {len(targets)} 条")
    applied = 0
    for d, c in targets:
        print(f"  [HIT ] {d.get('title')}  {len(d.get('text') or '')} → {len(c)} 字")
        print(f"         新开头: {c[:60]}")
        if dry:
            continue
        docs.remove(d)
        doc = kb.add_doc(KB_ID, d.get("type"), d.get("title"), c, d.get("metadata") or {})
        if doc:
            applied += 1
            print(f"  [OK  ] 已重录 id={doc.get('id')}")
        else:
            print(f"  [FAIL] 重录失败，旧条目已摘除！请检查 {d.get('title')}")

    after = len(kb._get_mem(KB_ID).get("docs", []))
    print(f"清理后 {KB_ID} 条数 = {after}  (净变化 {after - before_total}，实际重录 {applied})")
    sys.exit(0)


if __name__ == "__main__":
    main()
