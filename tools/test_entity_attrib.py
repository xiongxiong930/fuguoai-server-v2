#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实体归属护栏（张冠李戴）纯函数单测。

不依赖 numpy / fuguoai_* 等重依赖：用 MagicMock 桩掉这些模块后直接 import tourism_kb，
调用模块级 _entity_attribution_core，验证：
  FP1 荆州古城同源多条目误报消除（修复 a）
  FP2 武汉东湖高新区误抽东湖消除（修复 b）
  REG 对比句「首义广场21.95万㎡，而中山公园74.62公顷」无误报
  TP1 真实跨条目错配仍命中（黄鹤楼说成晴川阁高度）
  TP2 真实跨条目错配仍命中（首义广场说成中山公园面积）
  A   同源同数不误报（黄鹤楼/黄鹤楼·登楼记）
  C   修饰语前置「100公顷的A景区」正确归属
  FP3/FP5 未检索/已检索巧合同数（恩施30cm/排骨藕汤紧邻）不误报
  FP4 自归属优先（张居正街1公里，荆州古城仅方位描述）不误报
  TP4 真错配仍命中（荆州古城无1公里）
  FP6(C3-f) 主体洪湖莲藕无长度记录→30cm巧同荆州古城墙厚不误报
本地零 LLM、零容器，纯逻辑自验；提交进仓库作为回归护栏。
"""
import importlib.util
import sys
import types
from unittest.mock import MagicMock

# ---- 桩掉重依赖，使 tourism_kb 可在隔离环境 import ----
for mod in ("numpy", "fuguoai_config", "fuguoai_config.config_loader",
            "fuguoai_config.logger", "fuguoai_core", "fuguoai_core.providers",
            "fuguoai_core.providers.embedding", "fuguoai_core.utils",
            "fuguoai_core.utils.llm", "fuguoai_core.utils.metrics"):
    sys.modules.setdefault(mod, MagicMock())

_HERE = __file__
# 定位 tourism_kb.py（tools/ 同级 fuguoai_core/utils）
import os
_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                    "fuguoai_core", "utils", "tourism_kb.py")
_src = os.path.abspath(_src)
_spec = importlib.util.spec_from_file_location("tourism_kb_iso", _src)
tk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tk)

_extract = tk._extract_numbers
_core = tk._entity_attribution_core


def _tn(**kw):
    return {k: _extract(v) for k, v in kw.items()}


CASES = []


def check(name, answer, title_numbers, expect_n, note, retrieved_canon=None):
    got = _core(answer, title_numbers, retrieved_canon=retrieved_canon)
    ok = len(got) == expect_n
    CASES.append((name, ok, len(got), expect_n, note, got))
    return ok


# FP1: 荆州古城 同源多条目（短条目无长度，长条目有 11.28公里）→ 修复前应误报，修复后无
check("FP1-荆州古城同源", "荆州古城的古城墙周长约11.28公里，气势恢宏。",
      _tn(**{"荆州古城": "荆州古城是湖北保存最好的古城之一",
             "荆州古城·搜狐旅游资料": "荆州古城墙周长11.28公里，始建于春秋"}),
      0, "同源多条目归一，11.28公里 属于本 spot，不应判张冠李戴")

# FP2: 武汉东湖高新区 → 不应把「东湖」抽成《东湖》景区，1600米 属购物条目
check("FP2-东湖高新区误抽", "武汉东湖高新区附近购物很方便，距商业中心约1600米。",
      _tn(**{"东湖": "东湖水域面积33平方公里，是武汉最大城中湖",
             "武汉购物指南": "武汉光谷广场距鲁巷1600米，距街道口1350米，商业发达"}),
      0, "复合行政区「东湖高新区」跳过东湖匹配，1600米 不误归东湖")

# REG: 对比句两数字各自就近归属，无误报
check("REG-对比句", "首义广场21.95万㎡，而中山公园74.62公顷，各具特色。",
      _tn(**{"首义广场": "首义广场面积21.95万㎡",
             "中山公园": "中山公园面积74.62公顷"}),
      0, "对比句两数字各归其主，无误报")

# TP1: 黄鹤楼说成晴川阁高度（30米 是晴川阁的）→ 应命中
check("TP1-黄鹤楼错配晴川阁", "黄鹤楼高约30米，临江而立，是武汉地标。",
      _tn(**{"黄鹤楼": "黄鹤楼高约51.4米，位于蛇山",
             "晴川阁": "晴川阁高约30米，与黄鹤楼隔江相望"}),
      1, "真实跨条目错配（高度）应命中")

# TP2: 首义广场说成中山公园面积（74.62公顷 是中山公园的）→ 应命中
check("TP2-首义广场错配中山公园", "首义广场的面积约为74.62公顷，值得一游。",
      _tn(**{"首义广场": "首义广场面积21.95万㎡",
             "中山公园": "中山公园面积74.62公顷"}),
      1, "真实跨条目错配（面积）应命中")

# A: 同源同数不误报（黄鹤楼 / 黄鹤楼·登楼记 都记 51.4米）
check("A-同源同数", "黄鹤楼高约51.4米，是武汉地标。",
      _tn(**{"黄鹤楼": "黄鹤楼高约51.4米",
             "黄鹤楼·登楼记": "黄鹤楼始建于三国，高51.4米"}),
      0, "同源条目同数归一，不误报")

# (c) 验证：修饰语前置「100公顷的A景区」——数字在 POI 前，仍正确归属
check("C-修饰前置", "100公顷的东湖是武汉最大城中湖。",
      _tn(**{"东湖": "东湖水域面积100公顷，是武汉最大城中湖",
             "梁子湖": "梁子湖面积300平方公里"}),
      0, "数字前置其头名词，正确归属本 POI，无误报")

# ---- C3：retrieved_canon 收窄（消除未检索条目的巧合同数误报） ----
_tn_hong = _tn(**{
    "排骨藕汤": "排骨藕汤是湖北经典汤品，以猪小排与粉藕慢炖，汤色乳白",  # 本体无长度数字
    "洪湖莲藕": "洪湖莲藕孔洞分明，淀粉含量高，宜煲汤",
    "恩施大峡谷·景点简介（3）": "恩施大峡谷一炷香高约30厘米（石柱微缩模型）",  # 巧合同数，未被检索
})
# FP3：答案在《排骨藕汤》紧邻说 30厘米，所谓归属《恩施大峡谷》未被检索 → 收窄后 0 误报
check("FP3-未检索巧相同数(收窄)",
      "排骨藕汤用的是约30厘米的洪湖莲藕，粉糯好吃。",
      _tn_hong, 0,
      "retrieved_canon={排骨藕汤,洪湖莲藕}，恩施大峡谷未检索→不报张冠李戴",
      retrieved_canon={"排骨藕汤", "洪湖莲藕"})
# FP3-b：即便恩施大峡谷被检索到（retrieved_canon 含它），30厘米 紧邻 排骨藕汤 属其上下文
# 描述 → C3-c 紧邻归属仍抑制，不误报（旧行为依赖 canon 收窄；新行为更稳健）
check("FP5-已检索仍被紧邻归属抑制",
      "排骨藕汤用的是约30厘米的洪湖莲藕，粉糯好吃。",
      _tn_hong, 0,
      "retrieved_canon 含恩施大峡谷，但30厘米紧邻排骨藕汤→仍不误报",
      retrieved_canon={"排骨藕汤", "洪湖莲藕", "恩施大峡谷·景点简介（3）"})
# 旧行为对照（不传 retrieved_canon）：C3-c 紧邻归属同样抑制 → 0（证明收窄是补充而非唯一手段）
check("FP3-旧行为(无收窄)",
      "排骨藕汤用的是约30厘米的洪湖莲藕，粉糯好吃。",
      _tn_hong, 0,
      "不传 retrieved_canon→C3-c 紧邻归属仍抑制，不再报 1（行为已演进）")

# FP4：自归属优先——数字紧邻其真正归属 POI（张居正街…全长约1公里），虽前面还有
# 荆州古城（位于荆州古城内），仍应判为张居正街自身数据，不误报张冠李戴（C3-b 根因）
check("FP4-自归属优先(张居正街1公里)",
      "荆州古城内，张居正街全长约1公里，两侧是仿明清建筑。",
      _tn(**{"荆州古城": "荆州古城墙周长11.28公里，明清重建",
             "张居正街": "张居正街全长约1公里，为纪念张居正而建，仿明清风格"}),
      0, "1公里 属张居正街自身，荆州古城仅作方位描述，不误报")
# 反向：若答案真把 张居正街的1公里 安到 荆州古城（荆州古城无1公里），仍应命中（真实错配保留）
check("TP4-真错配仍命中(荆州古城无1公里)",
      "荆州古城全长约1公里，值得一游。",
      _tn(**{"荆州古城": "荆州古城墙周长11.28公里，明清重建",
             "张居正街": "张居正街全长约1公里，为纪念张居正而建，仿明清风格"}),
      1, "荆州古城自身无1公里→确为错配张冠李戴，仍命中")

# FP6（C3-f 根因）——洪湖莲藕莲藕段「30厘米」与荆州古城墙厚「30厘米」巧合同数，但主体
# 洪湖莲藕自身【无】长度记录，无法断定该数字错配（可能只是本 POI 未收录该计量），不应判张冠李戴。
# 线上实测捕获：数字「30厘米」出现在《洪湖莲藕》附近，但实为《荆州古城·搜狐旅游资料（2）》的
# 「长度」数据，疑似张冠李戴——修复后主体无此类记录直接跳过。
check("FP6-主体无此类记录不误报(洪湖莲藕30cm)",
      "洪湖莲藕的莲藕段长约30厘米，煲汤粉糯好吃。",
      _tn(**{"洪湖莲藕": "洪湖莲藕孔洞分明，淀粉含量高，宜煲汤，是湖北名产",  # 本体无长度数字
             "荆州古城·搜狐旅游资料（2）": "荆州古城墙厚约30厘米，为明代砖城"}),
      0, "主体洪湖莲藕无长度记录→无法断定30cm错配（荆州古城墙厚巧同）→不误报")


def main():
    passed = sum(1 for c in CASES if c[1])
    total = len(CASES)
    print("=" * 64)
    print("实体归属护栏单测：%d/%d 通过" % (passed, total))
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
