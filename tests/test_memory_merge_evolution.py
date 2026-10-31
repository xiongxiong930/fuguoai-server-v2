"""长期记忆持久合并单测（维度②E1，无网络/无 LLM）。

真进化验证：钉死"重建即遗忘"缺陷的修复——
旧行为 st["idx"] = entries 整体替换，某轮总结压缩掉的旧事实永久消失；
新行为 _merge_index 按 id upsert，旧事实保留率 100%。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fuguoai_core.providers.memory.mem_local_short.mem_local_short import (
    MemoryProvider,
)


def _mp():
    mp = MemoryProvider.__new__(MemoryProvider)  # 不走 __init__，纯逻辑测试
    return mp


def _e(eid, etype="fact", value="v", ts="2026-07-01", weight=1.0, keywords=None):
    return {
        "id": eid,
        "type": etype,
        "key": eid,
        "value": value,
        "keywords": keywords or [],
        "ts": ts,
        "weight": weight,
    }


def test_old_facts_survive():
    """核心修复点：新一轮总结丢掉的旧事实必须保留（旧行为=丢失）。"""
    mp = _mp()
    old = [_e("a", value="生日6月1日"), _e("b", value="忌口花生")]
    new = [_e("c", value="养了猫")]  # 新总结完全没提 a/b
    merged = mp._merge_index(old, new)
    ids = {x["id"] for x in merged}
    assert ids == {"a", "b", "c"}, ids
    assert next(x for x in merged if x["id"] == "a")["value"] == "生日6月1日"


def test_upsert_updates_value_keeps_ts_bumps_weight():
    """同 id：新值覆盖，ts 保留首次，weight +0.1（重复提及加权）。"""
    mp = _mp()
    old = [_e("a", value="住北京", ts="2026-06-01", weight=1.0)]
    new = [_e("a", value="住上海", ts="2026-07-30", weight=1.0)]
    merged = mp._merge_index(old, new)
    assert len(merged) == 1
    m = merged[0]
    assert m["value"] == "住上海"       # 更新语义
    assert m["ts"] == "2026-06-01"      # 首次时间保留
    assert abs(m["weight"] - 1.1) < 1e-9


def test_weight_capped_at_2():
    mp = _mp()
    old = [_e("a", weight=1.95)]
    merged = mp._merge_index(old, [_e("a")])
    assert merged[0]["weight"] == 2.0
    merged = mp._merge_index(merged, [_e("a")])
    assert merged[0]["weight"] == 2.0


def test_capacity_eviction_pins_identity_preference():
    """超容量按 (weight, ts) 驱逐最轻最旧；identity/preference 常驻豁免。"""
    mp = _mp()
    cap = MemoryProvider._MAX_IDX_ENTRIES
    old = [_e(f"f{i}", ts=f"2026-01-{(i % 28) + 1:02d}", weight=1.0) for i in range(cap)]
    old.append(_e("me", etype="identity", value="姓名: 熊", weight=1.0))
    new = [_e("hot", weight=2.0)]  # 高权重新事实
    merged = mp._merge_index(old, new)
    assert len(merged) == cap
    ids = {x["id"] for x in merged}
    assert "me" in ids      # identity 豁免驱逐
    assert "hot" in ids     # 高权重保留
    assert len([x for x in merged if x["type"] == "identity"]) == 1


def test_merge_handles_garbage_input():
    """脏输入（None/{}/非 dict 条目/缺 id）不崩，尽力合并。"""
    mp = _mp()
    assert mp._merge_index(None, None) == []
    assert mp._merge_index({}, []) == []
    merged = mp._merge_index([_e("a")], ["garbage", {"no_id": 1}, _e("b")])
    assert {x["id"] for x in merged} == {"a", "b"}


def test_score_entries_weight_boosts_rank():
    """检索排名：同等词法匹配下 weight 高者排前（重复提及优先召回）。"""
    mp = _mp()
    e_low = _e("low", value="喜欢喝咖啡", weight=1.0, keywords=["咖啡"])
    e_high = _e("high", value="喜欢喝咖啡", weight=2.0, keywords=["咖啡"])
    ranked = mp._score_entries([e_low, e_high], "咖啡")
    assert ranked[0]["id"] == "high"


if __name__ == "__main__":
    test_old_facts_survive()
    test_upsert_updates_value_keeps_ts_bumps_weight()
    test_weight_capped_at_2()
    test_capacity_eviction_pins_identity_preference()
    test_merge_handles_garbage_input()
    test_score_entries_weight_boosts_rank()
    print("MEMORY_MERGE_TESTS_PASS")
