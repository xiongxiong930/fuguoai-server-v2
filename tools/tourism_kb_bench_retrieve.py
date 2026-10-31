# -*- coding: utf-8 -*-
"""文旅 KB 检索性能基准（优化前后对比用）。

测什么：hybrid_retrieve 的纯检索耗时（不含 LLM），并输出 top1 标题与 sim/kw，
用于验证"优化后检索结果不变"。

跑法（容器内 /opt/fuguoai-server，embedding 缓存只在容器有）：
  python3 tools/tourism_kb_bench_retrieve.py [每问重复次数]
"""
import asyncio
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not os.path.isdir(os.path.join(_ROOT, "fuguoai_core")):
    _ROOT = os.environ.get("FUGUOAI_ROOT", "/opt/fuguoai-server")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fuguoai_config.config_loader import load_config  # noqa: E402
from fuguoai_core.utils.tourism_kb import get_tourism_kb  # noqa: E402

QUERIES = [
    "黄鹤楼有多高", "武当山为什么是道教圣地", "恩施大峡谷怎么玩",
    "武汉有什么好吃的", "神农架几月份去最好", "襄阳牛肉面有什么来历",
    "武昌鱼是什么鱼", "荆州古城墙多长", "东湖绿道有多长",
    "咸宁桂花什么时候开", "潜江小龙虾哪里吃", "明显陵是谁的陵墓",
    "三峡大坝怎么参观", "湖北有哪些世界遗产", "黄陂木兰草原好玩吗",
    "随州银杏谷最佳观赏期", "宜昌到神农架怎么走", "黄石国家矿山公园门票",
    "鄂州梁子湖大闸蟹", "荆门有什么特产",
]


def main():
    reps = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    config = asyncio.run(load_config())
    from fuguoai_core.utils.config_holder import set_config
    set_config(config)
    kb = get_tourism_kb()
    if kb is None or not kb.enabled:
        print("ERROR: kb 未启用")
        sys.exit(2)

    n = len(kb._get_mem("hubei").get("docs", []))
    print(f"KB 条数 = {n}；每问重复 {reps} 次")

    # 预热（把 embedding 模型与首次检索开销排除在计时外）
    kb.hybrid_retrieve("hubei", QUERIES[0])

    results = {}
    t0 = time.perf_counter()
    for _ in range(reps):
        for q in QUERIES:
            kb.hybrid_retrieve("hubei", q)
    total = time.perf_counter() - t0
    calls = reps * len(QUERIES)
    avg = total / calls * 1000
    print(f"总耗时 {total:.3f}s / {calls} 次调用 => 平均 {avg:.2f} ms/次检索")

    # 结果快照：top1 标题 + sim/kw（用于验证优化不改变检索结果）
    print("--- 结果快照（top1）---")
    for q in QUERIES:
        r = kb.hybrid_retrieve("hubei", q)
        if not r:
            print(f"{q:<22} -> (空)")
            continue
        d = r[0]
        print(f"{q:<22} -> {d.get('title')} | sim={d.get('_sim')} kw={d.get('_kw')}")
        results[q] = (d.get("title"), d.get("_sim"), d.get("_kw"))
    return avg


if __name__ == "__main__":
    main()
