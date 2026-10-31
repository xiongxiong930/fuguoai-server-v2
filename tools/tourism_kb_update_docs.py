# -*- coding: utf-8 -*-
"""湖北文旅 KB 通用「按标题原地改写正文」工具。

⚠️ 为什么不能直接用 add_doc：
    add_doc 的文档 id = sha1(type|title|text)，**正文一变就是新 id**，
    直接 add_doc 只会新增一条、旧的错误/损坏条目仍在 ⇒ 同主题双份、检索可能命中旧垃圾。
    本工具做法：先按标题把旧条目从 docs 中摘除，再用新正文 add_doc（重新 embed + 落盘）。
    因 add_doc 内部读的是同一份 _mem 并整体 _save，删除与新增会一起持久化。

本次修复/增厚（2026-09-26，事实均经政府权威源核实）：
  1. 襄阳牛肉面 —— 原正文是**抓取损坏的乱码**（含“网球运动员/江苏卫视交友节目”等无关碎片与
     “导草该错笔械沿号差算”之类断字）。按襄阳市人民政府《襄阳特产02-襄阳牛肉面》改写为
     准确描述：碱面/窝子面/一辣二麻三鲜/牛油精髓/襄阳黄酒。
  2. 武昌鱼 —— 原文缺关键事实（是什么鱼、为何叫武昌鱼）。按鄂州市人民政府/市文联资料补充：
     学名团头鲂、属鳊鱼、俗称樊口鳊鱼、得名于古武昌（今鄂州）、1979《辞海》定名。
  3. 鄂州梁子湖大闸蟹与武昌鱼 —— 补樊口鳊鱼/团头鲂与“产樊口者甲天下”方志依据。

跑法（容器内 /opt/fuguoai-server，embedding 缓存只在容器有）：
  python3 tools/tourism_kb_update_docs.py
"""
import asyncio
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not os.path.isdir(os.path.join(_ROOT, "fuguoai_core")):
    _ROOT = os.environ.get("FUGUOAI_ROOT", "/opt/fuguoai-server")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fuguoai_config.config_loader import load_config  # noqa: E402
from fuguoai_core.utils.tourism_kb import get_tourism_kb  # noqa: E402

KB_ID = "hubei"

# ⚠️ 中文强调引号一律用全角 “ ” —— ASCII " 会截断 Python 字符串导致语法错误。
UPDATES = [
    {
        "match_title": "襄阳牛肉面",
        "new_text": (
            "襄阳牛肉面是襄阳的城市名片，也是大多数襄阳市民过早（吃早餐）的首选，已有三百多年历史。"
            "来历：始创于清朝康熙元年（公元1662年），创始人马和瑞为回族人，膝下三子马永生、马合仁、马德泰，"
            "他令三子伙同经营面馆，店铺名“永合泰”即取三子名字中各一字，寓意团结一心把祖传家业发扬壮大。"
            "清嘉庆年间，马永生把自家特制的卤牛肉加入面条之中，并将丁香、砂仁、桂皮、大料等几味中药融入"
            "配方，牛肉面由此成型并声名远播；二十世纪七十年代，永合泰牛肉面曾被用作国宴特色食品，"
            "2015年襄阳牛肉面启动申报国家非物质文化遗产。"
            "做法上又称“窝子面”，采用碱面煮制：碱面加豆芽放入俗称“窝子”的竹提漏中，在骨汤底里煮至面条"
            "筋道捞出，淋上红油和浇头，再撒香葱碎与香菜碎即可食用。其精髓在牛油——用牛骨头熬汤，"
            "十几味中药和调料融合植物油熬制佐料，再配上麻辣油，味道独特、营养丰富。"
            "特点是“一辣二麻三鲜”，味道可口、回味悠长，再佐一碗襄阳黄酒更为惬意。"
        ),
        "metadata_patch": {
            "source_site": "襄阳市人民政府",
            "source_url": "https://www.xiangyang.gov.cn/sq/xygk/xytc/202307/t20230714_3285009.shtml",
            "tags": ["襄阳", "牛肉面", "碱面", "窝子面", "一辣二麻三鲜", "牛油", "过早", "黄酒"],
        },
    },
    {
        "match_title": "武昌鱼",
        "new_text": (
            "武昌鱼是湖北名鱼，学名团头鲂，属鲤科鲂属，是鳊鱼的一种，俗称樊口鳊鱼。"
            "它得名于古武昌（今鄂州）：三国吴甘露元年（公元265年）东吴末帝孙皓迁都武昌（今鄂州市城区），"
            "童谣“宁饮建业水，不食武昌鱼”是最早记载。1955—1957年，鱼类学家易伯鲁将梁子湖—樊口种群"
            "科学定名为团头鲂，即正宗武昌鱼，核心产区为鄂州梁子湖与长江交汇水域；1979年《辞海》明确"
            "“武昌鱼，即团头鲂”。经典做法为清蒸武昌鱼，肉质细嫩、鲜味突出，1965年被正式定为湖北地方"
            "风味名菜，是鄂菜头牌，因“才饮长沙水，又食武昌鱼”的名句广为人知。"
        ),
        "metadata_patch": {
            "source_site": "鄂州市人民政府 / 鄂州市文联",
            "source_url": "https://www.ezhou.gov.cn/zjez/ezrw/eztc/202207/t20220721_486985.html",
            "tags": ["武昌鱼", "团头鲂", "鳊鱼", "樊口", "梁子湖", "鄂州", "清蒸", "鄂菜"],
        },
    },
    {
        "match_title": "江汉路步行街",
        "new_text": (
            "江汉路步行街位于武汉汉口，南起沿江大道江汉关，北至京汉大道，全长1600米，"
            "是汉口商业与金融繁荣的发祥地，始建于1861年；1927年以路南端的江汉关为名统称“江汉路”。"
            "2000年改建成商业步行街，享有“天下第一步行街”的美誉。街上自东向西一字排开欧陆、罗马、"
            "拜占庭、文艺复兴、古典主义与现代派等多种风格建筑，被誉为“二十世纪的建筑博物馆”；"
            "江汉关大楼等19处历史建筑、永康里等6处老里弄、亨达利钟表等10家百年老字号密集分布。"
            "2019年成为华中地区首条入选商务部全国步行街提升试点的街区，2021年入选第二批“全国示范步行街”，"
            "并拥有首批中国历史文化街区、国家级夜间文化和旅游消费集聚区等“国字号”招牌。"
            "地铁2号线、6号线贯通，是外地游客到武汉的打卡必到之地。"
        ),
        "metadata_patch": {
            "source_site": "武汉市商务局 / 武汉市江汉区人民政府",
            "source_url": "https://sw.wuhan.gov.cn/xwdt/mtbd/202505/t20250502_2576725.shtml",
            "tags": ["江汉路", "步行街", "武汉", "江汉关", "建筑博物馆", "全国示范步行街", "百年老街"],
        },
    },
    {
        "match_title": "咸宁桂花",
        "new_text": (
            "咸宁是全国桂花五大传统产区之一，尤以咸安区桂花镇为著，咸安桂花已有2000多年栽培历史。"
            "据史料记载，2300多年前战国时期诗人屈原途经咸宁写下“奠桂酒兮椒浆”“沛吾乘兮桂舟”的诗句；"
            "500年前民间就有酿制桂花美酒的传统。资源方面：全市现存地径5厘米以上桂花150万株，"
            "百年以上古桂2000株、占全国2200株的91%（其中200—500年1250株、500年以上150株、"
            "600年以上3株，最大树龄617年），桂花品种数量、古树数量、基地面积、鲜桂花产量与品质"
            "五项资源指标居全国领先地位。1963年、1983年国家两次命名咸宁为“桂花之乡”，"
            "2000年国家林业局与中国花卉协会命名咸安区为唯一的“中国桂花之乡”；"
            "2023年“湖北咸宁古桂花树群”入选第七批中国重要农业文化遗产。"
            "赏桂核心去处是咸安区桂花镇的桂花源景区（核心区约800亩），拥有国内最大的古桂群。"
        ),
        "metadata_patch": {
            "source_site": "咸宁市农业农村局 / 咸安区人民政府",
            "source_url": "http://nyj.xianning.gov.cn/nyzx/gzdt/202309/t20230922_3285519.shtml",
            "tags": ["桂花", "咸宁", "中国桂花之乡", "古桂", "桂花镇", "屈原", "桂花源"],
        },
    },
    {
        "match_title": "武汉步行街",
        "new_text": (
            "武汉的步行街以江汉路步行街、光谷步行街与楚河汉街最具代表性。"
            "江汉路步行街在汉口，全长1600米，始建于1861年，以江汉关大楼等近代建筑群被称为"
            "“二十世纪的建筑博物馆”，2021年入选第二批“全国示范步行街”；"
            "光谷步行街位于东湖高新区，全长1350米，融合多国建筑风格，是年轻人潮流与购物打卡地；"
            "楚河汉街位于武汉中央文化区，长约1.5公里，连接东湖与沙湖，沿河设名人广场，"
            "是购物休闲与夜游热门地。三条街分别对应百年商埠、青春潮流与滨河时尚三种不同气质。"
        ),
        "metadata_patch": {
            "tags": ["步行街", "江汉路", "光谷步行街", "楚河汉街", "武汉", "购物", "打卡"],
        },
    },
    {
        "match_title": "鄂州梁子湖大闸蟹与武昌鱼",
        "new_text": (
            "梁子湖水域是正宗武昌鱼（学名团头鲂，俗称樊口鳊鱼）的核心产区：梁子湖水自鄂城樊口镇注入长江，"
            "樊口即成为这种鱼的集散地；清光绪《武昌县志》“物产篇”载“鲂，即鳊鱼，名缩项鳊，产樊口者甲天下”，"
            "《湖北通志》亦称“鳊，各处通产，以武昌樊口为最”。湖区同时盛产梁子湖大闸蟹，秋蟹肥美；"
            "武昌鱼以清蒸为经典鄂菜。湖鲜宴是当地代表性吃法，注意时令与鲜活度。"
        ),
        "metadata_patch": {
            "tags": ["梁子湖", "大闸蟹", "武昌鱼", "团头鲂", "樊口鳊鱼", "樊口", "鄂州", "湖鲜"],
        },
    },
]


def main():
    config = asyncio.run(load_config())
    from fuguoai_core.utils.config_holder import set_config
    set_config(config)
    kb = get_tourism_kb()
    if kb is None or not kb.enabled:
        print("ERROR: tourism kb 未启用或单例为空（请确认 set_config 已注入配置）")
        sys.exit(2)

    data = kb._get_mem(KB_ID)
    docs = data.setdefault("docs", [])
    before = len(docs)
    print(f"改写前 {KB_ID} 条数 = {before}")

    total_removed = 0
    for u in UPDATES:
        mt = u["match_title"]
        # ⚠️ 默认精确匹配标题。子串匹配会误伤：如 match_title="武昌鱼" 会同时命中
        #   "鄂州梁子湖大闸蟹与武昌鱼"，把鄂州条目正文覆盖成通用武昌鱼正文
        #   （2026-09-26 实际踩到，靠后续规则自愈才没留下错误）。
        #   确需子串匹配时显式写 "substring": True。
        if u.get("substring"):
            matches = [d for d in docs if mt in (d.get("title") or "")]
        else:
            matches = [d for d in docs if (d.get("title") or "") == mt]
        if not matches:
            print(f"  [MISS] 未找到标题含 “{mt}” 的条目")
            continue
        for m in matches:
            print(f"  [OLD ] {m.get('title')} (id={m.get('id')}) 旧正文 {len(m.get('text') or '')} 字")
            docs.remove(m)
            total_removed += 1
            meta = dict(m.get("metadata") or {})
            meta.update(u.get("metadata_patch") or {})
            doc = kb.add_doc(
                KB_ID,
                u.get("type") or m.get("type"),
                u.get("new_title") or m.get("title"),
                u["new_text"],
                meta,
            )
            print(f"  [{'OK ' if doc else 'FAIL'}] {u.get('new_title') or m.get('title')} "
                  f"新正文 {len(u['new_text'])} 字" + (f" id={doc.get('id')}" if doc else ""))

    after = len(kb._get_mem(KB_ID).get("docs", []))
    print(f"改写后 {KB_ID} 条数 = {after}  (摘除 {total_removed}，重录 {total_removed}，净变化 {after - before})")
    sys.exit(0)


if __name__ == "__main__":
    main()
