# -*- coding: utf-8 -*-
"""TourismKB 黄金集回归评测（防胡说八道护栏）。

用途：每次改 KB / 改 query 阈值后跑一遍，抓两类回归：
  1) 强命中查询被错误兜底（应走 RAG 却落到「通用知识整理」）—— 漏检权威内容；
  2) 库外查询被错误当成命中（应兜底却硬编 KB 内容）—— 编造/张冠李戴。
  3) 命中查询的答案必须包含期望事实片段（RAG 真检索到、没答非所问）。

跑法（容器内 /opt/fuguoai-server，embedding 模型缓存只在容器有）：
  python3 tools/tourism_kb_eval.py
退出码非 0 = 有 FAIL；0 = 全 PASS。可当 CI 门禁。
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

FALLBACK_MARKER = "通用知识整理"

# kind: rag=应命中KB并含期望事实; mid=相关但非直击,必须留在RAG(不得兜底); fallback=库外,必须兜底
# 注：「恩施有什么特色美食」「襄阳有什么名小吃」等改写型查询实测 best 融合分 <0.55 会落到兜底
# （LLM 给的是正确通识、非编造，可接受，但绕过了策展 KB）—— 属已知边界，不强行断言，待 reranker(升级C) 改善。
CASES = [
    # —— RAG 强命中：答案须含期望事实，且 sources 不是兜底标记 ——
    {"q": "恩施土家族有哪些习俗和禁忌", "kind": "rag", "must": ["拦门酒"]},
    {"q": "咸宁桂花有什么典故", "kind": "rag", "must": ["屈原"]},
    # —— 中等相关（mid）：必须留在 RAG（sources 非兜底），不得误兜底；答案须含期望事实 ——
    # 襄阳牛肉面有什么来历 实测 best 融合分落在 0.55~0.70 → confidence=medium，正好验证中相关约束路径
    {"q": "襄阳牛肉面有什么来历", "kind": "mid", "not_fallback": True, "must": ["永合泰"]},
    # —— 库外兜底：sources 必须为兜底标记（证明库外问题能答且不冒充 KB 权威）——
    # ⚠️ 本条是【阈值 0.40 的临界震荡样本】，route 会随库内容变化在 RAG/兜底间来回切换
    # （2026-09-25 实测：加武汉条目→过线走 RAG；删一条后→又落回兜底）。故不锁 route，
    # 改用 kind=safe 只锁安全底线：兜底可接受（已标注非 KB 权威）；走 RAG 则必须明确否认，
    # 否则判为"拿不相关条目编造"。
    # 附带暴露的真问题（待用户拍板，属 P2/P3）：阈值 0.40 下，与查询主题不相关但语义/关键词
    # 相近的条目（武汉住宿/购物/门票）足以把无关查询推过阈值 → 可能用不相关内容作答。
    {"q": "武汉周边有哪些滑雪场", "kind": "safe",
     "must_any": ["未涉及滑雪场", "未提及.*滑雪", "没有.*滑雪场", "没有关于.*滑雪场",
                  "无法回答.*滑雪", "不.*滑雪场"]},
    # C5 跨域过滤回归：明确提到「非湖北」主要城市/地标的 query，应兜底（或 RAG 明确否认），
    # 绝不可用不相关湖北文档（如武当山「游玩攻略」）作答——防张冠李戴错答。
    {"q": "上海迪士尼游玩攻略", "kind": "safe",
     "must_any": ["不在湖北", "超出.*范围", "不在.*范围内", "不.*服务范围", "无法.*解答"]},
    {"q": "北京故宫门票多少钱", "kind": "safe",
     "must_any": ["不在湖北", "超出.*范围", "不在.*范围内", "不.*服务范围", "没有.*信息"]},
    # 阈值 0.40 后：宜昌海滨浴场这类「库内可推理否认」查询回归 RAG（用宜昌文档正确否认），
    # 不再走通用 LLM；答案须明确否认海滨浴场（防止被错误断言有海滨浴场）。
    {"q": "宜昌有哪些海滨浴场", "kind": "rag",
     "must_any": ["未涉及海滨浴场", "未提及.*海滨", "未出现.*海滨", "没有.*海滨浴场", "无法回答.*海滨", "不.*海滨浴场"]},
]


def check(case, res):
    kind = case["kind"]
    sources = res.get("sources", [])
    answer = res.get("answer", "")
    conf = res.get("confidence", "?")
    fails = []

    if kind == "rag":
        if sources == [FALLBACK_MARKER]:
            fails.append("误兜底(应命中却落通用知识整理)")
        for m in case.get("must", []):
            if m not in answer:
                fails.append(f"缺期望事实「{m}」")
        if "must_any" in case and not any(re.search(p, answer) for p in case["must_any"]):
            fails.append("缺任一期望事实(%s)" % "/".join(case["must_any"]))
    elif kind == "mid":
        if case.get("not_fallback") and sources == [FALLBACK_MARKER]:
            fails.append("误兜底(中等相关查询不应落通用知识整理)")
        for m in case.get("must", []):
            if m not in answer:
                fails.append(f"缺期望事实「{m}」")
        if "must_any" in case and not any(re.search(p, answer) for p in case["must_any"]):
            fails.append("缺任一期望事实(%s)" % "/".join(case["must_any"]))
    elif kind == "fallback":
        if sources != [FALLBACK_MARKER]:
            fails.append(f"未兜底(sources={sources})")
    elif kind == "safe":
        # 安全底线型：不锁 route（该查询融合分在阈值附近会随库内容变化而在 RAG/兜底间切换），
        # 只锁安全底线——兜底(通用知识整理，已明示非 KB 权威)可接受；
        # 若走了 RAG，则必须用库内容明确否认，否则视为"拿不相关条目编造库内没有的事实"。
        fb = (sources == [FALLBACK_MARKER])
        denied = any(re.search(p, answer) for p in case.get("must_any", []))
        if not fb and not denied:
            fails.append(
                "走 RAG 却未否认(既非兜底也未明确否认，疑用不相关条目编造库内没有的事实)")
        for m in case.get("must", []):
            if m not in answer:
                fails.append(f"缺期望事实「{m}」")

    return conf, sources, answer, fails


def main():
    # 与服务启动一致：load_config 只填自身缓存，不会注入共享 holder；
    # 必须显式 set_config 让 _global_config() 拿到真实配置（否则 enabled=False）。
    config = asyncio.run(load_config())
    from fuguoai_core.utils.config_holder import set_config
    set_config(config)
    kb = get_tourism_kb()
    if kb is None or not kb.enabled:
        print("ERROR: tourism kb 未启用或单例为空（请确认 set_config 已注入配置）")
        sys.exit(2)

    n_fail = 0
    print(f"{'QUERY':<28} {'KIND':<9} {'CONF':<8} {'RESULT':<6} SOURCES")
    print("-" * 100)
    for c in CASES:
        res = kb.query("hubei", c["q"])
        conf, sources, answer, fails = check(c, res)
        ok = not fails
        n_fail += 0 if ok else 1
        tag = "PASS" if ok else "FAIL"
        src_disp = ",".join(sources) if sources else "(empty)"
        print(f"{c['q']:<26} {c['kind']:<9} {conf:<8} {tag:<6} {src_disp}")
        if ok:
            print(f"    answer[:80]={answer[:80]!r}")
        else:
            for f in fails:
                print(f"    [FAIL] {f}")
            print(f"    answer[:160]={answer[:160]!r}")
    print("-" * 100)
    print("RERANK 回归（城市感知重排：城市级查询 top1 须命中正确城市，跨城 POI 不污染 top3）")
    print("-" * 100)
    rerank_cases = [
        {"q": "十堰市中心有什么广场", "city": "十堰", "not_in_top3": ["洪山广场", "首义广场"]},
        {"q": "黄冈有什么湖值得去", "city": "黄冈", "not_in_top3": ["东湖"]},
        {"q": "咸宁哪里可以爬山", "city": "咸宁", "not_in_top3": ["东湖"]},
        {"q": "襄阳晚上去哪逛街", "city": "襄阳", "not_in_top3": []},
    ]
    for c in rerank_cases:
        docs = kb.retrieve_reranked("hubei", c["q"])
        top1 = docs[0].get("metadata", {}).get("city", "?") if docs else "?"
        titles3 = [d["title"] for d in docs[:3]]
        fails = []
        if top1 != c["city"]:
            fails.append(f"top1城市={top1}(期望{c['city']})")
        for bad in c["not_in_top3"]:
            if bad in titles3:
                fails.append(f"跨城污染:{bad}在top3")
        ok = not fails
        n_fail += 0 if ok else 1
        print(f"{c['q']:<24} top1={top1:<6} {'PASS' if ok else 'FAIL'} {'' if ok else ' '.join(fails)}")

    # ---- recall 回归：阈值 0.40 后，原先被 0.55 挡在兜底外的「KB 有答案」查询须回归 RAG 且含期望事实 ----
    print("-" * 100)
    print("RECALL 回归（放宽阈值后，KB 有答案的中等相关查询须回归 RAG 且答案含事实）")
    print("-" * 100)
    recall_cases = [
        {"q": "黄冈有什么湖值得去", "must": ["遗爱湖"], "not_fallback": True},
        {"q": "恩施带娃玩什么好", "must": ["女儿城"], "not_fallback": True},
        {"q": "武汉到神农架怎么走", "must": ["神农架"], "not_fallback": True},
    ]
    for c in recall_cases:
        res = kb.query("hubei", c["q"])
        conf = res.get("confidence", "?")
        answer = res.get("answer", "")
        sources = res.get("sources", [])
        fails = []
        if c.get("not_fallback") and sources == ["通用知识整理"]:
            fails.append("仍走兜底(应回归 RAG)")
        for m in c.get("must", []):
            if m not in answer:
                fails.append(f"缺期望事实「{m}」")
        ok = not fails
        n_fail += 0 if ok else 1
        print(f"{c['q']:<22} conf={conf:<7} {'PASS' if ok else 'FAIL'} {'' if ok else ' '.join(fails)}")

    print("-" * 100)
    print("NUMERIC 回归（数值精度护栏：答案数字须与检索上下文同单位数值一致，不得编造/美化）")
    print("-" * 100)
    # 直接调用护栏逻辑（不依赖 LLM），覆盖：同值异写/单位换算应通过；错值/编造计量类别应命中
    numeric_cases = [
        {"ans": "该园占地约75公顷，面积广阔", "src": ["占地74.62公顷"], "expect_flag": False,
         "desc": "同值四舍五入(公顷)应通过"},
        {"ans": "绿道全长约105km", "src": ["全长105公里"], "expect_flag": False,
         "desc": "单位换算 公里↔km 应通过"},
        {"ans": "景区约0.75平方公里", "src": ["占地74.62公顷"], "expect_flag": False,
         "desc": "单位换算 公顷↔km² 应通过"},
        {"ans": "该园占地120公顷", "src": ["占地74.62公顷"], "expect_flag": True,
         "desc": "错值(120 vs 74.62 公顷)应命中不符"},
        {"ans": "绿道全长30公里", "src": ["占地74.62公顷"], "expect_flag": True,
         "desc": "编造计量类别(长度)应命中未检索到"},
        {"ans": "该建筑2000年建成", "src": ["1993年建成"], "expect_flag": True,
         "desc": "年份错值(2000 vs 1993)应命中不符"},
        {"ans": "该建筑始于1993年", "src": ["1993年建成"], "expect_flag": False,
         "desc": "年份同值应通过"},
    ]
    for c in numeric_cases:
        flags = kb._numeric_faithfulness_check("hubei", c["ans"], c["src"])
        hit = bool(flags)
        ok = (hit == c["expect_flag"])
        n_fail += 0 if ok else 1
        detail = "" if ok else (" flags=%s" % flags)
        print(f"{c['desc']:<28} {'FLAG' if hit else 'ok':<6} {'PASS' if ok else 'FAIL'}{detail}")

    print("-" * 100)
    print("ENTITY 回归（实体级归属校验：防「A 的数字安到 B」张冠李戴；直接驱动护栏逻辑，自建最小 title_numbers）")
    print("-" * 100)
    # title_numbers 结构同 _extract_numbers 输出：{POI名: [{value(归一化), cls, raw, unit}, ...]}
    _AREA = lambda v, raw: [{"value": v, "cls": "area", "raw": raw, "unit": "公顷"}]
    entity_cases = [
        {"ans": "首义广场占地74.62公顷，面积广阔", "map": {
            "中山公园": _AREA(746200.0, "74.62公顷"),
            "首义广场": _AREA(219500.0, "21.95万㎡")},
         "expect_flag": True, "desc": "首义广场安上中山公园的74.62公顷应命中"},
        {"ans": "中山公园占地74.62公顷，绿意盎然", "map": {
            "中山公园": _AREA(746200.0, "74.62公顷"),
            "首义广场": _AREA(219500.0, "21.95万㎡")},
         "expect_flag": False, "desc": "数字正确归属自身应通过"},
        {"ans": "东湖绿道全长105公里，磨山也很美", "map": {
            "东湖": [{"value": 105000.0, "cls": "len", "raw": "105公里", "unit": "公里"}]},
         "expect_flag": False, "desc": "单POI数字正确归属应通过"},
        {"ans": "首义广场21.95万㎡，而中山公园74.62公顷", "map": {
            "中山公园": _AREA(746200.0, "74.62公顷"),
            "首义广场": _AREA(219500.0, "21.95万㎡")},
         "expect_flag": False, "desc": "双向各自归属正确应通过(无错配)"},
    ]
    for c in entity_cases:
        flags = kb._entity_attribution_check("hubei", c["ans"], title_numbers=c["map"])
        hit = bool(flags)
        ok = (hit == c["expect_flag"])
        n_fail += 0 if ok else 1
        detail = "" if ok else (" flags=%s" % flags)
        print(f"{c['desc']:<44} {'FLAG' if hit else 'ok':<6} {'PASS' if ok else 'FAIL'}{detail}")

    print("-" * 100)
    print(f"TOTAL={len(CASES) + len(rerank_cases) + len(recall_cases) + len(numeric_cases) + len(entity_cases)} FAIL={n_fail}")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
