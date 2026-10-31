# -*- coding: utf-8 -*-
"""实用信息批（第三波）入库后验证：类别分布 + 新类别召回抽查。

跑法（容器内）：python3 tools/verify_practical.py
- 打印 hubei.json 当前总数与类别分布（确认 stay/ticket/season/tip/shop 已入库）
- 对 5 个新类别各抽 1 条高频问法，走真实 kb.query，打印 conf / 是否兜底 / 首条来源 / 答案摘要
"""
import asyncio
import collections
import json
import os
import sys

_ROOT = "/opt/fuguoai-server"
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fuguoai_config.config_loader import load_config  # noqa: E402
from fuguoai_core.utils.tourism_kb import get_tourism_kb  # noqa: E402
from fuguoai_core.utils.config_holder import set_config  # noqa: E402

QS = [
    ("去武汉旅游住哪个片区方便", "stay"),
    ("黄鹤楼门票多少钱，开放时间是什么时候", "ticket"),
    ("湖北几月份去旅游最好", "season"),
    ("神农架徒步要注意什么安全事项", "tip"),
    ("湖北有什么特产值得带回家", "shop"),
]


def main():
    config = asyncio.run(load_config())
    set_config(config)
    kb = get_tourism_kb()

    with open("/opt/fuguoai-server/data/tourism_kb/hubei.json") as f:
        d = json.load(f)
    docs = d["docs"] if isinstance(d, dict) else d
    c = collections.Counter(x.get("type", "?") for x in docs)
    print(f"TOTAL={len(docs)}")
    for k, v in sorted(c.items()):
        print(f"  {k:<12} {v}")
    print("=" * 60)

    n_fb = 0
    for q, t in QS:
        res = kb.query("hubei", q)
        src = res.get("sources", [])
        ans = (res.get("answer", "") or "").replace("\n", " ")
        fb = (src == ["通用知识整理"])
        if fb:
            n_fb += 1
        print(f"[Q:{t}] {q}")
        print(f"   conf={res.get('confidence', '?')}  兜底={fb}")
        print(f"   src1={(src[0] if src else '(empty)')}")
        print(f"   ans={ans[:200]}")
        print("-" * 60)
    print(f"兜底条数 = {n_fb} / {len(QS)}（期望 0，即新条目已把查询从兜底拉回 RAG）")


if __name__ == "__main__":
    main()
