# -*- coding: utf-8 -*-
"""修正实用信息批（2026-09-25 第三波）的 type 落库错误 + 去重。

背景（踩坑复盘）：add_doc 有类型白名单校验（tourism_kb.py:279
    doc_type = doc_type if doc_type in _KB_TYPES else "other"）
本批入库时 _KB_TYPES 尚未登记 ticket/season/tip/shop，导致 13 条被静默归为 "other"
（仅 stay 在白名单内而正确落库），且因 fid=sha1(type|title|text) 依赖 type，
"湖北最佳旅游季节" 与库中既有同名 info 条目未判重 → 重复入库一条。

本脚本修正：
1. 白名单已同步扩展（tourism_kb.py 的 _KB_TYPES 加入 ticket/season/tip/shop）。
2. 把 12 条 other 改回正确 type，并按新 type 重算 id，保持与 add_doc 判重规则一致
   （避免下次同内容入库时因 type 变化再插一条）。
3. 删除重复项 [other] 湖北最佳旅游季节——保留库中既有的 [info] 湖北最佳旅游季节
   （既有数据不动，只删本批造成的重复；滑雪月份等信息在《神农架与恩施最佳季节》中仍有）。

跑法（容器内）：python3 tools/fix_practical_types.py
"""
import asyncio
import copy
import hashlib
import os
import sys

_ROOT = "/opt/fuguoai-server"
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fuguoai_config.config_loader import load_config  # noqa: E402
from fuguoai_core.utils.tourism_kb import get_tourism_kb  # noqa: E402
from fuguoai_core.utils.config_holder import set_config  # noqa: E402

# title -> 正确 type（不含被删除的重复项）
FIX = {
    "黄鹤楼门票与开放时间": "ticket",
    "武当山门票与开放时间": "ticket",
    "神农架门票与开放时间": "ticket",
    "恩施大峡谷门票与开放时间": "ticket",
    "武汉樱花季": "season",
    "神农架与恩施最佳季节": "season",
    "湖北山区徒步安全贴士": "tip",
    "三峡游轮与汛期安全": "tip",
    "冬季山区自驾与道路安全": "tip",
    "湖北伴手礼特产": "shop",
    "武汉商圈购物": "shop",
    "荆楚文创与非遗手作": "shop",
}

# 本批造成的重复项（title, type）→ 删除
DROP = ("湖北最佳旅游季节", "other")


def main():
    config = asyncio.run(load_config())
    set_config(config)
    kb = get_tourism_kb()
    if kb is None or not kb.enabled:
        print("ERROR: tourism kb 未启用")
        sys.exit(2)

    data = kb._get_mem("hubei")
    docs = data.setdefault("docs", [])
    before = len(docs)

    changed, dropped = [], []
    kept = []
    with kb._io_lock:
        for d in docs:
            t = d.get("title", "")
            ty = d.get("type", "")
            if (t, ty) == DROP:
                dropped.append(t)
                continue
            want = FIX.get(t)
            if want and ty != want:
                d["type"] = want
                # 重算 id，与 add_doc 的 fid 规则保持一致
                d["id"] = hashlib.sha1(
                    f"{want}|{t}|{d.get('text', '')}".encode("utf-8")
                ).hexdigest()[:16]
                changed.append(f"{t}: {ty} -> {want}")
            kept.append(d)
        docs[:] = kept
        snapshot = copy.deepcopy(data)
    kb._save("hubei", snapshot)

    print(f"条数 {before} -> {len(docs)}")
    print(f"改 type {len(changed)} 条：")
    for c in changed:
        print("  ", c)
    print(f"删除重复 {len(dropped)} 条：{dropped}")

    # 修正后分布
    import collections
    cnt = collections.Counter(x.get("type", "?") for x in docs)
    print("当前分布：", dict(sorted(cnt.items())))


if __name__ == "__main__":
    main()
