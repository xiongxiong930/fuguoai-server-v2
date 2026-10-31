# -*- coding: utf-8 -*-
"""文旅 KB 查询链路组件级性能剖析（非 LLM 部分）。

用途：定位"除了 LLM 之外，每次问答还剩哪些开销"，避免凭感觉优化。
分别计时：hybrid_retrieve / rerank / _all_kb_titles / 三道 faithfulness 护栏。

跑法（容器内 /opt/fuguoai-server，embedding 缓存只在容器有）：
  python3 tools/tourism_kb_profile_query.py [每问重复次数]
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


def _timeit(fn, reps, queries):
    t0 = time.perf_counter()
    for _ in range(reps):
        for q in queries:
            fn(q)
    return (time.perf_counter() - t0) / (reps * len(queries)) * 1000


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
    print(f"KB 条数 = {n}；{len(QUERIES)} 问 × {reps} 轮")

    # 预热
    kb.retrieve_reranked("hubei", QUERIES[0])

    def do_retrieve(q):
        kb.hybrid_retrieve("hubei", q, top_k=max(kb.top_k, kb.rerank_candidate_k))

    def do_rerank(q):
        pool = kb.hybrid_retrieve("hubei", q, top_k=max(kb.top_k, kb.rerank_candidate_k))
        kb.rerank("hubei", q, pool, top_k=kb.top_k)

    def do_titles(_q):
        kb._all_kb_titles("hubei")

    # 用检索到的文本拼一个"拟真答案"，再跑三道护栏
    def _fake_answer(q):
        pool = kb.hybrid_retrieve("hubei", q, top_k=kb.top_k)
        return " ".join(d.get("text", "")[:200] for d in pool)

    answers = {q: _fake_answer(q) for q in QUERIES}

    def do_faith(q):
        pool = kb.hybrid_retrieve("hubei", q, top_k=kb.top_k)
        src = [d["title"] for d in pool]
        kb._faithfulness_check("hubei", answers[q], src)

    def do_numeric(q):
        pool = kb.hybrid_retrieve("hubei", q, top_k=kb.top_k)
        kb._numeric_faithfulness_check("hubei", answers[q], [d.get("text", "") for d in pool])

    def do_entity(q):
        pool = kb.hybrid_retrieve("hubei", q, top_k=kb.top_k)
        src = [d["title"] for d in pool]
        kb._entity_attribution_check("hubei", answers[q], ctx_titles=src)

    print("--- 组件平均耗时（ms/次，含各自内部的检索调用）---")
    for name, fn in [
        ("hybrid_retrieve", do_retrieve),
        ("rerank(+retrieve)", do_rerank),
        ("_all_kb_titles", do_titles),
        ("_faithfulness_check(+retrieve)", do_faith),
        ("_numeric_faithfulness_check(+retrieve)", do_numeric),
        ("_entity_attribution_check(+retrieve)", do_entity),
    ]:
        print(f"  {name:<42} {_timeit(fn, reps, QUERIES):8.3f} ms")


if __name__ == "__main__":
    main()
