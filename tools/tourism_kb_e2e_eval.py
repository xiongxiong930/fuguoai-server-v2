# -*- coding: utf-8 -*-
"""文旅知识库【端到端评估基线】（P3）。

与 tools/tourism_kb_eval.py 的分工：
- tourism_kb_eval.py = 单元/回归护栏（23 例，断言式，PASS/FAIL，用于防回归）
- 本脚本 = 端到端基线大盘（57 例真实问法，量化指标，用于回答"库现在到底好不好用"）：
  命中率 / 兜底率 / faithfulness 触发率（护栏真实触发，非 LLM 自带免责声明），并列出明细供逐条归因。

指标口径：
- 命中 hit：答案正则命中 expect_any 任一（证明答到了点子上）
- 兜底 fb：sources == ["通用知识整理"]（库没接住，走了通用 LLM）—— 不依赖 expect 措辞，硬指标
- 有效命中：hit 且 非兜底（真正由策展 KB 接住并答对）
- faithfulness 触发：guard 真正追加「仅供参考」警示（res.faithfulness=="caution"）。
  ⚠️ 与 LLM 自身良性免责声明（如"票价仅供参考，以官网为准"）区分——后者不计入，
  否则会把正常免责语误判成护栏误报。LLM 自带免责语单独统计 caution_llm。

⚠️ expect_any 设计纪律（踩过坑）：**期望词必须排除查询词本身**。
初版把"黄鹤楼""遗爱湖""湖""安全"等泛词/查询词写进 expect，答案必然重复查询词 ⇒
命中率虚高到 100%、完全失去区分度。本版 expect 只保留「查询中未出现、且是答案应给出的
增量事实/专名」，命中率才有意义。

跑法（容器内，embedding 缓存只在容器有）：
  python3 tools/tourism_kb_e2e_eval.py
"""
import asyncio
import collections
import os
import re
import sys
import time

_ROOT = "/opt/fuguoai-server"
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fuguoai_config.config_loader import load_config  # noqa: E402
from fuguoai_core.utils.tourism_kb import get_tourism_kb  # noqa: E402
from fuguoai_core.utils.config_holder import set_config  # noqa: E402

FALLBACK_MARKER = "通用知识整理"

# (query, 类别, expect_any 正则列表) —— expect 不含查询词本身，只含增量事实
CASES = [
    # ---------- scenic（景点）----------
    ("黄鹤楼有什么来历", "scenic", ["崔颢", "李白", "江南名楼", "蛇山", "始建于", "屡毁屡建", "1985"]),
    ("武当山为什么是道教圣地", "scenic", ["真武", "太极", "张三丰", "宫观", "明成祖", "皇家", "紫霄", "金顶"]),
    ("神农架有什么值得看的", "scenic", ["神农顶", "大九湖", "金丝猴", "原始森林", "华中屋脊", "天燕", "官门山"]),
    ("东湖有哪些好玩的", "scenic", ["绿道", "樱园", "磨山", "行吟阁", "落雁", "听涛"]),
    ("湖北省博物馆镇馆之宝是什么", "scenic", ["曾侯乙", "编钟", "越王勾践剑", "元青花", "尊盘"]),
    ("武汉大学樱花什么时候开", "scenic", ["3月", "4月", "花期", "中旬", "下旬"]),
    ("三峡大坝在哪个城市", "scenic", ["宜昌", "三斗坪", "秭归", "185", "高程"]),
    ("恩施大峡谷怎么游览", "scenic", ["七星寨", "云龙地缝", "一炷香", "绝壁长廊", "索道", "5小时"]),
    ("荆州古城有什么看头", "scenic", ["城墙", "宾阳楼", "关羽", "张居正", "瓮城", "护城河"]),
    ("襄阳古隆中和诸葛亮什么关系", "scenic", ["躬耕", "三顾茅庐", "刘备", "隐居", "卧龙"]),
    ("明显陵是谁的陵墓", "scenic", ["嘉靖", "明世宗", "朱祐杬", "兴献王", "一陵双冢"]),
    ("赤壁古战场在哪里", "scenic", ["咸宁", "赤壁市", "周瑜", "曹操", "摩崖石刻"]),
    ("木兰文化生态旅游区有什么", "scenic", ["木兰山", "木兰草原", "黄陂", "花木兰", "云雾山"]),
    ("恩施土家女儿城有什么特色", "scenic", ["女儿会", "相亲", "摔碗酒", "土家", "民俗街"]),
    ("晴川阁和古琴台的典故", "scenic", ["伯牙", "子期", "知音", "高山流水", "钟子期"]),
    ("昙华林是什么地方", "scenic", ["老街", "历史建筑", "教堂", "文艺", "花园山"]),
    ("黄冈遗爱湖怎么样", "scenic", ["苏东坡", "东坡", "城市湖泊", "休闲", "11.9", "公顷"]),
    ("恩施土司城看什么", "scenic", ["九进堂", "钟楼", "风雨桥", "廪君", "土家族"]),
    # ---------- food（美食）----------
    ("武汉热干面有什么特点", "food", ["芝麻酱", "碱水面", "萝卜丁", "过早", "拌"]),
    ("户部巷有什么小吃", "food", ["豆皮", "面窝", "糊汤粉", "热干面", "小吃街"]),
    ("襄阳牛肉面好吃在哪", "food", ["碱面", "牛油", "一辣二麻三鲜", "辣", "麻"]),
    ("武昌鱼是什么鱼", "food", ["鳊鱼", "樊口", "梁子湖", "毛泽东", "才饮长沙水"]),
    ("荆州鱼糕怎么做", "food", ["鱼糜", "鱼肉", "淀粉", "蒸制", "切片"]),
    ("恩施玉露茶是什么茶", "food", ["蒸青", "针形", "绿茶", "1680", "松针"]),
    ("洪湖莲藕有什么名堂", "food", ["藕汤", "七孔", "九孔", "淀粉", "水生"]),
    ("钟祥蟠龙菜的来历", "food", ["嘉靖", "御膳", "吃肉不见肉", "红薯", "蛋皮"]),
    # ---------- festival（节庆）----------
    ("武当太极大会是什么", "festival", ["2024", "太极拳", "全球", "论道", "十堰"]),
    ("土家女儿会是什么样的节日", "festival", ["东方情人节", "对歌", "相亲", "农历七月"]),
    ("屈原故里端午有什么活动", "festival", ["秭归", "龙舟", "祭屈", "非遗", "习俗"]),
    ("武汉马拉松什么时候办", "festival", ["2016", "首届", "春季", "汉马", "4月"]),
    ("咸宁温泉有什么特色", "festival", ["温泉之都", "地热", "康养", "避寒", "度假"]),
    # ---------- transport（交通）----------
    ("武汉天河机场情况", "transport", ["T3", "华中", "2017", "航线", "4F"]),
    ("武当山机场什么时候通航", "transport", ["2016", "2月5日", "通航", "十堰"]),
    ("武汉高铁有哪些站", "transport", ["武汉站", "汉口站", "武昌站", "京广", "沪汉蓉"]),
    ("三峡游轮从哪里登船", "transport", ["宜昌", "茅坪", "太平溪", "登船", "母港"]),
    # ---------- stay（住宿）----------
    ("去武汉住哪个片区方便", "stay", ["武昌", "汉口", "光谷", "地铁", "片区"]),
    ("武当山住山上还是山下", "stay", ["山上", "山下", "南岩", "琼台", "特区", "日出"]),
    ("去恩施玩住哪里合适", "stay", ["市区", "利川", "建始", "鹤峰", "女儿城"]),
    ("宜昌住哪里方便坐游轮", "stay", ["滨江", "商圈", "码头", "秭归港"]),
    # ---------- ticket（票务）----------
    ("黄鹤楼门票多少钱", "ticket", ["70", "35", "08:30", "18:00", "夜游", "150"]),
    ("神农架门票怎么买划算", "ticket", ["联票", "299", "269", "159", "130", "大九湖"]),
    ("恩施大峡谷门票和游览时间", "ticket", ["七星寨", "105", "云龙地缝", "50", "索道", "5小时"]),
    # ---------- season（季节）----------
    ("湖北几月份去旅游最好", "season", ["春秋", "3-5月", "9-11月", "3月", "4月", "10月", "11月", "避暑", "滑雪"]),
    ("神农架什么时候去最合适", "season", ["避暑", "6-8月", "彩林", "10月", "滑雪", "12月"]),
    # ---------- tip（安全贴士）----------
    ("神农架徒步要注意什么", "tip", ["防滑", "保暖", "步道", "核心区", "温差", "结伴"]),
    ("三峡游轮汛期要注意什么", "tip", ["汛期", "6-8月", "水位", "航班", "身份证件", "船方"]),
    ("冬天去山区自驾要注意什么", "tip", ["结冰", "防滑链", "减速", "团雾", "雪场", "日照"]),
    # ---------- shop（购物）----------
    ("湖北有什么特产值得带", "shop", ["玉露", "脐橙", "香菇", "周黑鸭", "麻糖", "港饼"]),
    ("武汉去哪里逛街购物", "shop", ["江汉路", "楚河汉街", "光谷", "武广", "商圈"]),
    ("湖北有什么文创伴手礼", "shop", ["汉绣", "漆器", "缠花", "艾灸", "编钟", "省博"]),
    # ---------- culture / poem / info / route ----------
    ("汉口和武汉三镇是怎么来的", "culture", ["三镇", "武昌", "汉阳", "街巷", "由来", "租界"]),
    ("恩施土家族有什么习俗禁忌", "culture", ["习俗", "禁忌", "土家族", "哭嫁", "傩戏", "图腾"]),
    ("念奴娇赤壁怀古是谁写的", "poem", ["苏轼", "大江东去", "周郎", "羽扇纶巾"]),
    ("伯牙鼓琴讲的是什么故事", "poem", ["子期", "知音", "古琴台", "高山流水", "钟子期"]),
    ("湖北交通出行有什么建议", "info", ["高铁", "机场", "九省通衢", "铁路", "公路"]),
    ("恩施大峡谷游玩线路怎么安排", "route", ["线路", "七星寨", "云龙地缝", "索道", "地面缆车"]),
    ("武汉两江夜游有什么推荐", "route", ["知音号", "两江四岸", "夜景", "游船", "灯光秀"]),
]


def main():
    config = asyncio.run(load_config())
    set_config(config)
    kb = get_tourism_kb()
    if kb is None or not kb.enabled:
        print("ERROR: tourism kb 未启用")
        sys.exit(2)

    n = len(CASES)
    hit_n = fb_n = eff_n = caution_n = caution_llm_n = 0
    miss, fb_list, caution_list, caution_llm_list = [], [], [], []
    by_cat = collections.defaultdict(lambda: {"n": 0, "hit": 0, "fb": 0})
    t0 = time.time()

    for q, cat, pats in CASES:
        res = kb.query("hubei", q)
        src = res.get("sources", []) or []
        ans = (res.get("answer", "") or "").replace("\n", " ")
        fb = (src == [FALLBACK_MARKER])
        hit = any(re.search(p, ans) for p in pats)
        faith = res.get("faithfulness", "grounded")
        # 护栏真实触发（须人工看是否误报）；与 LLM 自带"仅供参考"免责声明区分：
        # 后者（如"票价仅供参考，以官网为准"）是良性免责语，不计入护栏误报。
        caution = (faith != "grounded")
        caution_llm = ("仅供参考" in ans and faith == "grounded")

        by_cat[cat]["n"] += 1
        if hit:
            hit_n += 1
            by_cat[cat]["hit"] += 1
        else:
            miss.append((cat, q, ans[:120]))
        if fb:
            fb_n += 1
            by_cat[cat]["fb"] += 1
            fb_list.append((cat, q))
        if hit and not fb:
            eff_n += 1
        if caution:
            caution_n += 1
            caution_list.append((cat, q))
        if caution_llm:
            caution_llm_n += 1
            caution_llm_list.append((cat, q))

    el = time.time() - t0
    print("=" * 72)
    print(f"文旅 KB 端到端基线：样本 {n} 条，耗时 {el:.0f}s")
    print("=" * 72)
    print(f"命中(答到增量事实)  : {hit_n}/{n}  = {hit_n / n:.0%}")
    print(f"有效命中(KB 接住)   : {eff_n}/{n}  = {eff_n / n:.0%}   ← 核心指标")
    print(f"兜底(走通用 LLM)    : {fb_n}/{n}  = {fb_n / n:.0%}   ← 硬指标(不依赖措辞)")
    print(f"faithfulness 触发   : {caution_n}/{n} = {caution_n / n:.0%}（护栏真实追加警示，须人工看是否误报）")
    print(f"LLM 自带免责声明     : {caution_llm_n}/{n} = {caution_llm_n / n:.0%}（良性，不计入护栏误报）")
    print("-" * 72)
    print("分类别：")
    for cat in sorted(by_cat):
        s = by_cat[cat]
        print(f"  {cat:<10} n={s['n']:<3} 命中={s['hit']:<3} 兜底={s['fb']:<3}")
    if fb_list:
        print("-" * 72)
        print(f"兜底明细（{len(fb_list)} 条，库没接住）：")
        for c, q in fb_list:
            print(f"  [{c}] {q}")
    if miss:
        print("-" * 72)
        print(f"未命中明细（{len(miss)} 条，未答到增量事实）——逐条归因用：")
        for c, q, a in miss:
            print(f"  [{c}] {q}")
            print(f"      -> {a}")
    if caution_list:
        print("-" * 72)
        print(f"faithfulness 触发明细（{len(caution_list)} 条，护栏真实触发，人工判是否误报）：")
        for c, q in caution_list:
            print(f"  [{c}] {q}")
    if caution_llm_list:
        print("-" * 72)
        print(f"LLM 自带免责声明明细（{len(caution_llm_list)} 条，良性，不计入护栏误报）：")
        for c, q in caution_llm_list:
            print(f"  [{c}] {q}")
    print("=" * 72)
    print(f"SUMMARY n={n} hit={hit_n} eff={eff_n} fallback={fb_n} caution={caution_n} caution_llm={caution_llm_n}")


if __name__ == "__main__":
    main()
