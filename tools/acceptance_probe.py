#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复果AI 自进化能力 · 真机级验收探针（在 159 生产容器内运行）

运行方式（进入容器后）：
    PYTHONPATH=/opt/fuguoai-server python3 /tmp/acceptance_probe.py

作用边界：
- 本探针驱动 **真实部署的代码 + 真实 config.yaml + 真实磁盘 data/**，
  把维度②(persona/记忆) / 维度④(eval) / 维度⑤(家庭RAG) 的真实逻辑跑通，
  并验证数据持久化与 live /metrics 端点。
- 物理设备说话/出声那一步属于"真机手动验收"，不在此探针范围（见报告文档）。
- 小程序管理 API(persona/family HTTP) 带 M16 用户态 JWT 守卫；M16 验证器未注入时
  返回 401，故这些 API 的"验收"通过 chat() 直接复用的数据层(PersonaStore/FamilyRAG)
  验证，而非走 HTTP（详见报告）。

退出码：任一验收项失败返回 1，全过返回 0。
"""
import os
import sys
import json
import asyncio
import urllib.request

sys.path.insert(0, "/opt/fuguoai-server")

from fuguoai_config.config_loader import get_project_dir
import yaml

from fuguoai_core.utils.persona import get_persona_store
from fuguoai_core.utils.family_rag import get_family_rag
from fuguoai_core.providers.memory.mem_local_short.mem_local_short import MemoryProvider
from fuguoai_core.utils.dialogue_eval import get_dialogue_eval
from fuguoai_core.providers.tools.unified_tool_manager import _sanitize_error
from fuguoai_core.utils.metrics import METRICS

TEST_DEV = "acc-test-device"   # 归一化后为 "acctestdevice"，独立于生产数据
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}" + (f"  —— {detail}" if detail else ""))


def _cval(snap, name):
    """标签感知地取计数器总和（METRICS 现带 labels，不能用空标签 () 查）。"""
    d = snap.get(name) or {}
    return sum(d.values()) if d else 0


def _gval(snap, name, default=5):
    d = snap.get(name) or {}
    return min(d.values()) if d else default


def load_config():
    cfg_path = os.path.join(get_project_dir(), "config.yaml")
    with open(cfg_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
class MockJudge:
    """确定性 judge：_set_test_llm 注入，绕过真实 LLM。"""
    def __init__(self, overall, safety=5):
        self._blob = json.dumps({
            "relevance": overall, "helpfulness": overall,
            "safety": safety, "coherence": overall, "overall": overall,
        })

    def response_no_stream(self, system_prompt=None, user_prompt=None):
        yield self._blob


def section_persona(cfg):
    print("\n[维度② 人格 Persona]")
    store = get_persona_store(cfg)
    check("PersonaStore 单例可用", store is not None)
    check("persona 功能已启用(enabled)", store.enabled, f"enabled={store.enabled}")
    # 设一个测试设备的自定义人格
    store.set_persona(TEST_DEV, {"name": "小福", "style": "活泼、简短", "catchphrase": "包在我身上"})
    block = store.render(TEST_DEV)
    check("render 产出 <persona> 块", "<persona>" in block, repr(block[:40]))
    check("自定义自称生效(name=小福)", "小福" in block)
    check("自定义口头禅生效", "包在我身上" in block)
    merged = store.get_persona(TEST_DEV)
    check("三层合并含自定义字段", merged.get("name") == "小福")
    # 持久化校验：直接读 data/.persona.yaml
    ppath = os.path.join(get_project_dir(), "data", ".persona.yaml")
    on_disk = False
    if os.path.exists(ppath):
        with open(ppath, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        # persona store 存盘用原始 device_id(不做归一化)，故查 TEST_DEV 原值
        on_disk = TEST_DEV in data and data[TEST_DEV].get("name") == "小福"
    check("人格持久化到 data/.persona.yaml", on_disk)
    # 清理
    store.set_persona(TEST_DEV, {})
    print("  ↩ 已清理测试设备人格")


def section_family_rag(cfg):
    print("\n[维度⑤ 家庭RAG FamilyRAG]")
    rag = get_family_rag(cfg)
    check("FamilyRAG 单例可用", rag is not None)
    check("FamilyRAG 功能已启用(enabled)", rag.enabled, f"enabled={rag.enabled}")
    # 录入一条家庭事实
    fact = rag.upsert_fact(TEST_DEV, "我家猫叫咪咪，是一只橘猫", "pet", ["猫", "咪咪", "橘猫"])
    check("upsert_fact 写入成功", fact is not None and fact.get("id"))
    facts = rag.list_facts(TEST_DEV)
    check("list_facts 返回刚录入的事实", len(facts) == 1 and "咪咪" in facts[0]["content"])
    # 语义召回
    got = rag.retrieve(TEST_DEV, "猫叫什么名字")
    check("retrieve 按语义召回家庭事实", any("咪咪" in g for g in got), f"召回={got}")
    block = rag.render(TEST_DEV, "猫叫什么名字")
    check("render 产出 <family_knowledge> 块", "<family_knowledge>" in block)
    # 持久化校验
    fpath = os.path.join(get_project_dir(), "data", "family_rag", "acctestdevice.json")
    on_disk = os.path.exists(fpath)
    check("家庭事实持久化到 data/family_rag/{did}.json", on_disk)
    # 清理
    n = rag.clear(TEST_DEV)
    check("clear 清空测试设备事实", n >= 1)
    print("  ↩ 已清理测试设备家庭知识")


def section_memory_merge(cfg):
    print("\n[维度② 长期记忆·记忆重建即遗忘修复(⑮E1)]")
    prov = MemoryProvider(cfg, "")
    e1 = prov._mk_entry("identity", "name", "姓名: 张三", ["张三"])
    e2 = prov._mk_entry("fact", "忌口", "不吃香菜", ["香菜"])
    old = [e1, e2]
    # 新的一轮 LLM 总结时把 e2(香菜)压缩丢了，只留 e1
    new = [e1]
    merged = prov._merge_index(old, new)
    ids = {e["id"] for e in merged}
    check("旧事实(香菜忌口)在合并后保留", e2["id"] in ids,
          f"保留条目数={len(merged)}")
    check("重建即遗忘缺陷已修复(旧事实不丢)", e2["id"] in ids and e1["id"] in ids)
    # 重复提及加权
    merged2 = prov._merge_index(merged, new)
    w = {e["id"]: e["weight"] for e in merged2}.get(e1["id"], 1.0)
    check("重复提及权重累加(封顶2.0)", w >= 1.1, f"weight(e1)={w}")
    # 结构化检索召回模拟
    prov._state[TEST_DEV] = {"mem": "", "idx": merged}
    recall = asyncio.run(prov.query_memory("他不吃什么", role_id=TEST_DEV))
    check("结构化索引可检索(香菜被召回)", "香菜" in recall, repr(recall[:50]))


def section_eval(cfg):
    print("\n[维度④ 对话自评估 DialogueEval(㉑ 评分闭环)]")
    ev = get_dialogue_eval(cfg)
    check("DialogueEval 单例可用", ev is not None)
    check("dialogue_eval 已启用(enabled)", ev.enabled, f"enabled={ev.enabled}")
    METRICS._counters.clear()
    METRICS._gauges.clear()
    # 正常回答 -> 高分，不应触发低分告警
    ev._set_test_llm(MockJudge(overall=5, safety=5))
    s = ev.evaluate(TEST_DEV, "A", "今天天气怎么样", "今天晴，适合出门。", cfg)
    check("正常回答打分返回(overall=5)", s and s.get("overall") == 5)
    check("高分不触发低分告警",
          "fuguoai_eval_low_quality_total" not in METRICS.snapshot_counters())
    # 低质回答 -> overall=1，应触发低分告警
    ev._set_test_llm(MockJudge(overall=1, safety=5))
    ev.evaluate(TEST_DEV, "A", "今天天气怎么样", "啊。", cfg)
    c = METRICS.snapshot_counters()
    check("低分触发 fuguoai_eval_low_quality_total",
          _cval(c, "fuguoai_eval_low_quality_total") > 0)
    check("最近总体分 gauge 已写入(<5)",
          _gval(METRICS.snapshot_gauges(), "fuguoai_eval_last_overall", 5) <= 3)
    # 安全红线 -> safety=1
    METRICS._counters.clear()
    ev._set_test_llm(MockJudge(overall=5, safety=1))
    ev.evaluate(TEST_DEV, "A", "如何做危险事", "详细步骤...", cfg)
    c = METRICS.snapshot_counters()
    check("安全红线触发 fuguoai_eval_safety_alerts_total",
          _cval(c, "fuguoai_eval_safety_alerts_total") > 0)


def section_tool_sanitize():
    print("\n[维度⑳ 工具调用错误脱敏]")
    raw = "KeyError: 'api_key'\n  File \"x.py\", line 1, in <module>\n    do()"
    safe = _sanitize_error(raw)
    check("_sanitize_error 不泄露密钥(api_key)", "api_key" not in safe, repr(safe))
    check("_sanitize_error 压成单行(去traceback)", "\n" not in safe)
    long = "x" * 500
    # 超长时函数返回 redacted[:limit] + "..."(limit=300, 故上限 303)
    check("_sanitize_error 过长截断", len(_sanitize_error(long)) <= 303)


def section_live_metrics():
    print("\n[Live /metrics 端点(运行中的服务器)]")
    prefixes = (
        "fuguoai_eval_total", "fuguoai_eval_low_quality_total",
        "fuguoai_eval_safety_alerts_total", "fuguoai_eval_last_overall",
        "fuguoai_family_rag_facts", "fuguoai_family_rag_retrieves_total",
        "fuguoai_persona_injections_total", "fuguoai_tool_calls_total",
        "fuguoai_memory_rag_hits_total", "fuguoai_memory_structured_entries",
    )
    try:
        with urllib.request.urlopen("http://127.0.0.1:8002/metrics", timeout=5) as r:
            body = r.read().decode("utf-8", "replace")
        check("/metrics 端点可达(HTTP 200)", r.status == 200)
        present = [p for p in prefixes if p in body]
        # 计数器首次 inc 才注册；刚重启/无流量时自进化计数器为 0 或未出现属正常，
        # 故此处只确认命名空间已暴露(常驻 gauge fuguoai_memory_structured_entries 必在)。
        check("自进化指标命名空间已在 /metrics 暴露", len(present) >= 1,
              f"已暴露 {len(present)}/{len(prefixes)} 个自进化指标")
        if present:
            print("    暴露的指标: " + ", ".join(present))
        # 报告当前已被真实流量点亮(>0)的自进化计数器
        lit = []
        for line in body.splitlines():
            if line.startswith("fuguoai_") and not line.startswith("#"):
                name = line.split("{")[0]
                val = line.split()[-1] if " " in line else "0"
                if name in prefixes and val not in ("0", "0.0"):
                    lit.append(f"{name}={val}")
        if lit:
            print("    🔆 当前已点亮: " + ", ".join(lit))
        else:
            print("    (当前无自进化计数器>0 — 需真实对话点亮，属正常)")
    except Exception as e:
        check("/metrics 端点可达", False, f"异常: {e}")


def main():
    print("=" * 64)
    print("复果AI 自进化能力 · 真机级验收探针")
    print("=" * 64)
    try:
        cfg = load_config()
    except Exception as e:
        print(f"❌ 无法加载 config.yaml: {e}")
        sys.exit(1)
    print(f"config: enabled(persona)={cfg.get('persona',{}).get('enabled')} "
          f"FamilyRAG={cfg.get('FamilyRAG',{}).get('enabled')} "
          f"dialogue_eval={cfg.get('dialogue_eval',{}).get('enabled')}")

    section_persona(cfg)
    section_family_rag(cfg)
    section_memory_merge(cfg)
    section_eval(cfg)
    section_tool_sanitize()
    section_live_metrics()

    print("\n" + "=" * 64)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"验收结果: {passed}/{total} 通过")
    fails = [n for n, ok, _ in RESULTS if not ok]
    if fails:
        print("未通过项: " + "; ".join(fails))
        print("=" * 64)
        sys.exit(1)
    print("全部通过 ✅")
    print("=" * 64)
    sys.exit(0)


if __name__ == "__main__":
    main()
