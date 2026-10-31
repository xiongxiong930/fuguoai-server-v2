# -*- coding: utf-8 -*-
"""文旅 KB：LLM 上下文成本量化 + 冷启动耗时测量（只读，不改库）。

回答两个问题：
  1) 每次问答到底喂了多少上下文给 LLM（字符数 / 条数 / 单条长度分布）？
     —— 用来判断「上下文裁剪」值不值得做、能降多少。
  2) 容器/进程重启后第一次检索的冷启动成本是多少（建索引 + ensure_vectors）？

跑法（容器内，embedding 缓存只在容器有）：
  python3 tools/tourism_kb_ctx_cost.py
"""
import asyncio
import os
import statistics
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not os.path.isdir(os.path.join(_ROOT, "fuguoai_core")):
    _ROOT = os.environ.get("FUGUOAI_ROOT", "/opt/fuguoai-server")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fuguoai_config.config_loader import load_config  # noqa: E402
from fuguoai_core.utils.tourism_kb import get_tourism_kb  # noqa: E402
from fuguoai_core.utils.config_holder import set_config  # noqa: E402

QUERIES = [
    "黄鹤楼有多高", "武当山为什么是道教圣地", "恩施大峡谷怎么玩",
    "武汉有什么好吃的", "神农架几月份去最好", "襄阳牛肉面有什么来历",
    "武昌鱼是什么鱼", "荆州古城墙多长", "东湖绿道有多长",
    "咸宁桂花什么时候开", "潜江小龙虾哪里吃", "明显陵是谁的陵墓",
    "三峡大坝怎么参观", "湖北有哪些世界遗产", "黄陂木兰草原好玩吗",
    "随州银杏谷最佳观赏期", "宜昌到神农架怎么走", "黄石国家矿山公园门票",
    "鄂州梁子湖大闸蟹", "荆门有什么特产",
]

TRUNC_LEVELS = (1200, 800, 500)


def _ctx_chars(kb, docs):
    """复刻 query() 里拼 ctx 的算法，返回字符数。"""
    return sum(len(f"[{i + 1}] 《{d['title']}》（{d.get('type', '')}）\n{d['text']}")
               for i, d in enumerate(docs))


def main():
    config = asyncio.run(load_config())
    set_config(config)
    kb = get_tourism_kb()
    if kb is None or not kb.enabled:
        print("ERROR: kb 未启用")
        sys.exit(2)

    did = "hubei"
    docs_all = kb._get_mem(did).get("docs", [])
    print(f"KB 条数 = {len(docs_all)}；top_k = {kb.top_k}；rerank_candidate_k = {kb.rerank_candidate_k}")

    # ---------- 1) 正文长度分布 ----------
    lens = sorted(len(d.get("text", "")) for d in docs_all)
    def _pct(p):
        return lens[min(len(lens) - 1, int(len(lens) * p))]
    print("\n[正文长度分布] 字")
    print(f"  min={lens[0]}  p25={_pct(0.25)}  p50={_pct(0.5)}  p75={_pct(0.75)}"
          f"  p90={_pct(0.9)}  max={lens[-1]}  均值={statistics.mean(lens):.0f}")
    over = [d for d in docs_all if len(d.get("text", "")) > 1200]
    print(f"  >1200 字的条目 = {len(over)} 条（占 {len(over) / len(docs_all) * 100:.1f}%）")
    for d in sorted(over, key=lambda x: -len(x.get("text", "")))[:8]:
        print(f"    - {len(d.get('text', '')):>5} 字  {d.get('title')}")

    # ---------- 2) 冷启动 ----------
    kb._retr_index.pop(did, None)
    kb._title_num_cache.pop(did, None)
    kb._migrate_ts.pop(did, None)
    t0 = time.perf_counter()
    kb.hybrid_retrieve(did, QUERIES[0], top_k=max(kb.top_k, kb.rerank_candidate_k))
    cold = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    kb.hybrid_retrieve(did, QUERIES[1], top_k=max(kb.top_k, kb.rerank_candidate_k))
    warm = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    kb._title_numbers(did, kb._get_mem(did).get("docs", []))
    tn_build = (time.perf_counter() - t0) * 1000
    print(f"\n[冷启动] 首次检索(含建索引+ensure_vectors) = {cold:.1f} ms；"
          f"第二次(热) = {warm:.1f} ms；title_numbers 冷建 = {tn_build:.1f} ms")

    # ---------- 3) 每问上下文成本 ----------
    rows = []
    for q in QUERIES:
        pool = kb.hybrid_retrieve(did, q, top_k=max(kb.top_k, kb.rerank_candidate_k))
        docs = kb.rerank(did, q, pool, top_k=kb.top_k) if kb.rerank_enabled else pool[:kb.top_k]
        full = _ctx_chars(kb, docs)
        trunc = {}
        for lv in TRUNC_LEVELS:
            trunc[lv] = _ctx_chars(
                kb, [dict(d, text=(d.get("text", "")[:lv])) for d in docs])
        rows.append((q, len(docs), full, trunc))

    print("\n[每问喂给 LLM 的上下文] 字符数")
    print(f"  {'query':<22}{'条数':>4}{'实际':>8}" +
          "".join(f"{'≤' + str(l):>8}" for lv in TRUNC_LEVELS for l in [lv]))
    for q, n, full, trunc in rows:
        print(f"  {q:<22}{n:>4}{full:>8}" +
              "".join(f"{trunc[lv]:>8}" for lv in TRUNC_LEVELS))

    fulls = [r[2] for r in rows]
    print(f"\n  实际：均值 {statistics.mean(fulls):.0f} 字 / p90 {sorted(fulls)[int(len(fulls) * 0.9)]} 字 / max {max(fulls)} 字")
    base = sum(fulls)
    for lv in TRUNC_LEVELS:
        s = sum(r[3][lv] for r in rows)
        print(f"  单条截断 ≤{lv} 字：总量 {s} 字（降 {(1 - s / base) * 100:.1f}%），均值 {s / len(rows):.0f} 字/问")

    # ---------- 4) top_k 敏感性（只影响喂给 LLM 的条数） ----------
    print("\n[top_k 敏感性] 上下文总量")
    for k in (5, 4, 3):
        tot = 0
        for q in QUERIES:
            pool = kb.hybrid_retrieve(did, q, top_k=max(k, kb.rerank_candidate_k))
            docs = kb.rerank(did, q, pool, top_k=k) if kb.rerank_enabled else pool[:k]
            tot += _ctx_chars(kb, docs)
        print(f"  top_k={k}: 总量 {tot} 字（相对 top_k=5 降 {(1 - tot / base) * 100:.1f}%），均值 {tot / len(QUERIES):.0f} 字/问")


if __name__ == "__main__":
    main()
