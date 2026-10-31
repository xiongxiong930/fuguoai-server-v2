"""家庭 RAG 召回基准（真进化门禁：可度量改进）。

维度⑤升级 embedding 后，用一组「事实 + 自然问法」验证 top-k 召回率提升。
本地 TF-IDF 只能命中字面重叠；本地 sentence-transformers 能命中同义/近义改写。

用法（159 容器内）：
  python3 bench_family_rag_recall.py --embedding local
  python3 bench_family_rag_recall.py --embedding local-st
比较两者 top-3 召回率，证明升级带来可度量改进。
"""

import argparse
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fuguoai_core.utils.family_rag import FamilyRAG

# (事实, 类别, [应命中该事实的自然问法])
FACTS = [
    ("爸爸对花生过敏，吃花生会起疹子", "health",
     ["爸爸有什么过敏", "他对坚果过敏吗", "爸爸能不能吃花生"]),
    ("妈妈每周一去瑜伽课", "schedule",
     ["妈妈周几去瑜伽", "妈妈什么时候运动", "妈的健身安排"]),
    ("家里的橘猫叫咪咪，最爱吃冻干", "pet",
     ["猫叫什么名字", "咪咪喜欢吃什么", "我们家宠物什么品种"]),
    ("猫粮放在厨房左边第二个柜子里", "item",
     ["猫粮放哪了", "厨房哪里有猫粮", "喂猫的粮在哪"]),
    ("爷爷住在武汉，去年刚退休", "family",
     ["爷爷住在哪里", "爷爷退休了吗", "武汉是谁的家"]),
    ("宝宝晚上九点必须睡觉", "schedule",
     ["孩子几点睡觉", "宝宝作息是怎样的", "几点是睡觉时间"]),
    ("爸爸不喝咖啡，只喝绿茶", "preference",
     ["爸爸平时喝什么", "爸能喝咖啡吗", "爸的饮料偏好"]),
    ("姥姥下周三来家里住一周", "family",
     ["姥姥什么时候来", "姥姥来住几天", "外婆哪天到"]),
]
TOP_K = 3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--embedding", default="local")
    args = ap.parse_args()
    tmp = tempfile.mkdtemp()
    cfg = {"FamilyRAG": {"enabled": True, "embedding": {"type": args.embedding}}}
    rag = FamilyRAG(cfg)
    rag._dir = os.path.join(tmp, "data", "family_rag")
    dev = "bench-dev"
    for content, cat, _ in FACTS:
        rag.add_fact(dev, content, category=cat)

    hits = 0
    total = 0
    for content, cat, queries in FACTS:
        for q in queries:
            total += 1
            if content in rag.retrieve(dev, q):
                hits += 1
    recall = hits / total if total else 0
    print(
        f"[embedding={args.embedding}] top-{TOP_K} recall = "
        f"{hits}/{total} = {recall:.1%}"
    )
    return recall


if __name__ == "__main__":
    main()
