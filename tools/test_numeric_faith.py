#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数值精度护栏（_numeric_faithfulness_check）纯逻辑单测。

隔离 import tourism_kb（桩掉重依赖），直接构造 TourismKB 子类实例，验证：
  FP1 年份缺失不误报（C3：答「始建于223年」，检索上下文无年份→不报）
  FP2 温度缺失不误报（C3：答「昼夜温差10℃」，检索上下文无温度→不报）
  TP1 真实数值不符仍报（答「海拔5000米」，源记「3106米」→报不符）
  TP2 真实数值缺失仍报（硬计量 area/len/mass 缺失→仍报，保留编造检测）
本地零 LLM、零容器；提交进仓库作为回归护栏。
"""
import importlib.util
import os
import sys
from unittest.mock import MagicMock

for mod in ("numpy", "fuguoai_config", "fuguoai_config.config_loader",
            "fuguoai_config.logger", "fuguoai_core", "fuguoai_core.providers",
            "fuguoai_core.providers.embedding", "fuguoai_core.utils",
            "fuguoai_core.utils.llm", "fuguoai_core.utils.metrics"):
    sys.modules.setdefault(mod, MagicMock())

_SRC = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..",
    "fuguoai_core", "utils", "tourism_kb.py"))
_spec = importlib.util.spec_from_file_location("tourism_kb_iso", _SRC)
tk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tk)


class _KB(tk.TourismKB):
    """最小桩：只启用数值护栏，禁用其它 faith 分支与 LLM。"""
    def __init__(self):
        self.enabled = True
        self.faith_enabled = False
        self.faith_numeric_enabled = True
        self.faith_entity_enabled = False
        self.faith_caveat = True


CASES = []


def check(name, answer, source_texts, expect_n, note):
    kb = _KB()
    got = kb._numeric_faithfulness_check("hubei", answer, source_texts)
    ok = len(got) == expect_n
    CASES.append((name, ok, len(got), expect_n, note, got))
    return ok


# FP1：年份缺失（C3 排除 year 类缺失）→ 不报
check("FP1-年份缺失不报", "黄鹤楼始建于公元223年，屡毁屡建。",
      ["黄鹤楼是武汉地标，位于蛇山，高51.4米"], 0,
      "年份为常识，检索上下文无年份→C3 排除，不误报")

# FP2：温度缺失（C3 排除 temp 类缺失）→ 不报（神农架徒步 10℃ 根因）
check("FP2-温度缺失不报", "神农架徒步昼夜温差可达10℃，需保暖。",
      ["神农架徒步需注意防滑、结伴，核心区禁止进入"], 0,
      "温度为通用上下文，检索上下文无温度→C3 排除，不误报")

# TP1：真实数值不符（海拔）仍报
check("TP1-海拔不符仍报", "神农顶海拔约5000米，是华中屋脊。",
      ["神农顶海拔3106米，是华中第一峰"], 1,
      "答 5000米 与源 3106米 超容差→仍报不符（真实检测保留）")

# TP2：硬计量（面积）缺失仍报（保留编造计量检测）
check("TP2-面积缺失仍报", "东湖水域面积约500平方公里，是全国最大城中湖。",
      ["东湖是武汉著名城中湖，风景秀丽"], 1,
      "面积属硬计量，检索上下文无面积→仍报缺失（编造风险高，保留）")


def main():
    passed = sum(1 for c in CASES if c[1])
    total = len(CASES)
    print("=" * 64)
    print("数值精度护栏单测：%d/%d 通过" % (passed, total))
    print("=" * 64)
    for name, ok, got, exp, note, flags in CASES:
        tag = "PASS" if ok else "FAIL"
        print("[%s] %-22s got=%d exp=%d | %s" % (tag, name, got, exp, note))
        if not ok:
            for f in flags:
                print("        → %s" % f)
    print("=" * 64)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
