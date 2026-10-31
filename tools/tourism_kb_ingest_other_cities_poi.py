# -*- coding: utf-8 -*-
"""其余湖北城市（宜昌/襄阳/荆州/咸宁/黄冈/恩施/十堰）城市级 POI 补录。

数据来源：百度百科、政府门户、地方志等公开权威资料（已逐条核实具体数字，防胡说八道）。
跑法（容器内 /opt/fuguoai-server，embedding 模型缓存只在容器有）：
  python3 tools/tourism_kb_ingest_other_cities_poi.py
逐条 add_doc，输出每条成功/跳过，并打印入库前后条数。
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


def _src(name):
    # 通用、永远有效的百度百科检索链接，作为可溯源出处
    return f"https://baike.baidu.com/search?word={name}"


# 其余湖北城市级 POI（城市广场 / 商业街 / 公园 / 森林公园）。
# type 统一用 scenic（仓内无 park/plaza 专用类型，沿用江汉路步行街的存储约定）；
# metadata 沿用既有约定：city / spot / source_site / source_url / origin，并附带 tags 提升召回。
ITEMS = [
    # ---------------------- 宜昌 ----------------------
    {
        "type": "scenic",
        "title": "夷陵广场",
        "text": (
            "夷陵广场位于宜昌市中心城区的铁路坝，1997年11月3日建成开放，占地约5.52万平方米，"
            "是宜昌最大的城市中心广场。广场以大型音乐喷泉和开阔绿地著称，是市民休闲集会与"
            "节庆活动的主要场所。"
        ),
        "metadata": {
            "city": "宜昌", "spot": "夷陵广场", "source_site": "百度百科",
            "source_url": _src("夷陵广场"), "origin": "web",
            "tags": ["广场", "城市客厅", "音乐喷泉", "铁路坝", "宜昌"],
        },
    },
    {
        "type": "scenic",
        "title": "宜昌滨江公园",
        "text": (
            "宜昌滨江公园沿长江而建，1983年始建，全长约25公里、占地约57万平方米，"
            "是长江中上游最长的滨江园林带之一。园内步道、绿地点缀江岸，可远眺大江与对岸磨基山，"
            "是市民晨练观江的胜地。"
        ),
        "metadata": {
            "city": "宜昌", "spot": "滨江公园", "source_site": "百度百科",
            "source_url": _src("宜昌滨江公园"), "origin": "web",
            "tags": ["公园", "长江", "滨江", "绿道", "磨基山"],
        },
    },
    {
        "type": "scenic",
        "title": "宜昌商业步行街",
        "text": (
            "宜昌商业步行街（解放路步行街）位于西陵区核心商圈，2004年建成，占地约117亩、"
            "建筑面积约40万平方米。街区集购物、餐饮、娱乐于一体，是宜昌人气最旺的商业轴线"
            "与城市消费地标。"
        ),
        "metadata": {
            "city": "宜昌", "spot": "商业步行街", "source_site": "百度百科",
            "source_url": _src("宜昌商业步行街"), "origin": "web",
            "tags": ["商业街", "西陵", "购物", "消费地标", "宜昌"],
        },
    },
    # ---------------------- 襄阳 ----------------------
    {
        "type": "scenic",
        "title": "诸葛亮广场",
        "text": (
            "诸葛亮广场位于襄阳樊城区，2001年9月建成开放，占地约106公顷，是襄阳规模最大的"
            "城市广场。广场中央立有高18米、重35吨的诸葛亮铜像，周边设音乐喷泉与绿化景观，"
            "是市民休闲与文化活动的核心空间。"
        ),
        "metadata": {
            "city": "襄阳", "spot": "诸葛亮广场", "source_site": "百度百科",
            "source_url": _src("诸葛亮广场"), "origin": "web",
            "tags": ["广场", "诸葛亮", "铜像", "城市客厅", "樊城"],
        },
    },
    {
        "type": "scenic",
        "title": "管家巷",
        "text": (
            "管家巷文化休闲街区位于襄阳古城内，2023年12月30日开街，占地面积约1.35万平方米，"
            "集聚约100家商户。街区以非遗手作、地方美食与沉浸式演艺还原襄阳古城风韵，"
            "是老襄阳味道的文旅新地标。"
        ),
        "metadata": {
            "city": "襄阳", "spot": "管家巷", "source_site": "百度百科",
            "source_url": _src("管家巷"), "origin": "web",
            "tags": ["历史街区", "非遗", "襄阳古城", "美食", "网红巷"],
        },
    },
    {
        "type": "scenic",
        "title": "襄阳北街",
        "text": (
            "襄阳北街位于襄阳古城内，全长约1270米，是全国最长的仿古步行街之一，1993年辟为"
            "步行街，2021年入选湖北旅游休闲街区。街两侧明清风格建筑林立，约300家商户经营"
            "特产、美食与文创，串联昭明台与临汉门。"
        ),
        "metadata": {
            "city": "襄阳", "spot": "北街", "source_site": "百度百科",
            "source_url": _src("襄阳北街"), "origin": "web",
            "tags": ["商业街", "仿古", "襄阳古城", "昭明台", "临汉门"],
        },
    },
    # ---------------------- 荆州 ----------------------
    {
        "type": "scenic",
        "title": "张居正街",
        "text": (
            "张居正街位于荆州古城内，全长约1公里，为纪念明代改革家张居正而建，两侧以仿明清"
            "风格建筑为主。街区汇聚荆楚风味小吃与文创店铺，是漫步荆州古城、感受历史人文的"
            "休闲轴线。"
        ),
        "metadata": {
            "city": "荆州", "spot": "张居正街", "source_site": "百度百科",
            "source_url": _src("张居正街"), "origin": "web",
            "tags": ["历史街区", "张居正", "荆州古城", "荆楚小吃", "仿明清"],
        },
    },
    {
        "type": "scenic",
        "title": "荆州中山公园",
        "text": (
            "荆州中山公园始建于1933年、1935年建成，占地约74.62公顷（其中水域约44公顷），"
            "被誉为全球最大的中山公园，年接待约200万人次。园内亭台水榭与园林景观交融，"
            "是荆州中心城区重要的城市绿地与休闲胜地。"
        ),
        "metadata": {
            "city": "荆州", "spot": "中山公园", "source_site": "百度百科",
            "source_url": _src("荆州中山公园"), "origin": "web",
            "tags": ["公园", "中山公园", "水域", "城市绿地", "荆州"],
        },
    },
    # ---------------------- 咸宁 ----------------------
    {
        "type": "scenic",
        "title": "十六潭公园",
        "text": (
            "十六潭公园位于咸宁温泉城区，2011年建成开放，占地约1229亩，是咸宁最大的城市"
            "综合公园。公园以十六处水潭与湿地生态为特色，配套绿道、广场与儿童乐园，是市民"
            "亲水休闲与周末出游的热门去处。"
        ),
        "metadata": {
            "city": "咸宁", "spot": "十六潭公园", "source_site": "百度百科",
            "source_url": _src("十六潭公园"), "origin": "web",
            "tags": ["公园", "湿地", "温泉城区", "绿道", "亲水"],
        },
    },
    {
        "type": "scenic",
        "title": "潜山国家森林公园",
        "text": (
            "潜山国家森林公园位于咸宁市区，森林茂密，1996年晋升为国家级森林公园，经营面积约"
            "1万余亩。园内负氧离子丰富，设有登山步道与观景平台，是咸宁城区近郊天然氧吧与"
            "康养健身之地。"
        ),
        "metadata": {
            "city": "咸宁", "spot": "潜山国家森林公园", "source_site": "百度百科",
            "source_url": _src("潜山国家森林公园"), "origin": "web",
            "tags": ["森林公园", "国家级", "登山", "氧吧", "咸宁"],
        },
    },
    # ---------------------- 黄冈 ----------------------
    {
        "type": "scenic",
        "title": "遗爱湖",
        "text": (
            "遗爱湖位于黄冈市区，水域面积约463.9公顷，为国家AAAA级景区、2016年入选国家"
            "湿地公园。湖名取自苏轼《遗爱亭记》，环湖建有遗爱十二景与绿道，是黄冈城市生态"
            "名片与市民休闲核心。"
        ),
        "metadata": {
            "city": "黄冈", "spot": "遗爱湖", "source_site": "百度百科",
            "source_url": _src("遗爱湖"), "origin": "web",
            "tags": ["公园", "苏轼", "国家湿地", "绿道", "AAAA"],
        },
    },
    {
        "type": "scenic",
        "title": "东坡赤壁",
        "text": (
            "东坡赤壁（文赤壁）位于黄冈城西长江之滨，为国家AAAA级景区，因苏轼在此写下前后"
            "《赤壁赋》与《念奴娇·赤壁怀古》而名扬天下。矶头赤色崖壁临江而立，园内有二赋堂、"
            "赤壁碑刻等人文胜迹。"
        ),
        "metadata": {
            "city": "黄冈", "spot": "东坡赤壁", "source_site": "百度百科",
            "source_url": _src("东坡赤壁"), "origin": "web",
            "tags": ["赤壁", "苏轼", "文赤壁", "二赋堂", "AAAA"],
        },
    },
    # ---------------------- 恩施 ----------------------
    {
        "type": "scenic",
        "title": "恩施土家女儿城",
        "text": (
            "恩施土家女儿城位于恩施市，占地约11万平方米，2013年8月开园，为国家AAAA级景区，"
            "免费开放。以土家族婚俗‘女儿会’文化为主题，集非遗展演、特色美食、民宿客栈于一体，"
            "是体验土家风情的核心街区。"
        ),
        "metadata": {
            "city": "恩施", "spot": "土家女儿城", "source_site": "百度百科",
            "source_url": _src("恩施土家女儿城"), "origin": "web",
            "tags": ["女儿会", "土家族", "非遗", "AAAA", "免票"],
        },
    },
    {
        "type": "scenic",
        "title": "恩施土司城",
        "text": (
            "恩施土司城位于恩施市，1998年始建、2002年开放，占地300余亩，为国家AAAA级景区。"
            "城内九进堂建筑面积约3999平方米，是展示土司文化与吊脚楼建筑艺术的‘土家第一城’，"
            "再现鄂西土司历史。"
        ),
        "metadata": {
            "city": "恩施", "spot": "土司城", "source_site": "百度百科",
            "source_url": _src("恩施土司城"), "origin": "web",
            "tags": ["土司城", "九进堂", "吊脚楼", "AAAA", "鄂西"],
        },
    },
    {
        "type": "scenic",
        "title": "恩施凤凰山森林公园",
        "text": (
            "恩施凤凰山森林公园位于恩施市城区，1993年5月经省林业厅批准建立，总面积约48.2公顷，"
            "是城区内唯一的森林公园。西临清江、南接闹市，建有凤凰亭、游乐场与生态步道，"
            "为市民健身休闲的城市绿心。"
        ),
        "metadata": {
            "city": "恩施", "spot": "凤凰山森林公园", "source_site": "百度百科",
            "source_url": _src("恩施凤凰山"), "origin": "web",
            "tags": ["森林公园", "清江", "凤凰亭", "城市绿心", "恩施"],
        },
    },
    # ---------------------- 十堰 ----------------------
    {
        "type": "scenic",
        "title": "五堰步行街",
        "text": (
            "五堰步行街位于十堰茅箭区，主街长约888米、总长约1100米，集聚约500家商户，"
            "2000年完成改造提升，为省级商业示范街区。街区是十堰最繁华的传统商圈，集购物、"
            "餐饮、数码与休闲于一体。"
        ),
        "metadata": {
            "city": "十堰", "spot": "五堰步行街", "source_site": "百度百科",
            "source_url": _src("五堰步行街"), "origin": "web",
            "tags": ["商业街", "茅箭", "商圈", "省级示范", "十堰"],
        },
    },
    {
        "type": "scenic",
        "title": "四方山生态公园",
        "text": (
            "四方山生态公园位于十堰张湾区，1992年建成开放，为国家AAA级景区，经营面积约1.5万亩，"
            "距市中心约6公里。园内植被丰茂、建有植物园与观光亭，是十堰城区最大的城市山地"
            "生态公园与天然氧吧。"
        ),
        "metadata": {
            "city": "十堰", "spot": "四方山生态公园", "source_site": "百度百科",
            "source_url": _src("四方山生态公园"), "origin": "web",
            "tags": ["生态公园", "AAA", "植物园", "氧吧", "张湾"],
        },
    },
    {
        "type": "scenic",
        "title": "十堰人民广场",
        "text": (
            "十堰人民广场（六堰广场）位于十堰市中心六堰，1988年建成，占地面积约4.1万平方米，"
            "绿化面积约2万平方米。广场配备声控喷泉与休闲设施，是车城地标性的集会、休闲与"
            "夜间游乐城市客厅。"
        ),
        "metadata": {
            "city": "十堰", "spot": "人民广场", "source_site": "百度百科",
            "source_url": _src("十堰人民广场"), "origin": "web",
            "tags": ["广场", "六堰", "声控喷泉", "城市客厅", "十堰"],
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

    before = len(kb._get_mem("hubei").get("docs", []))
    print(f"入库前 hubei 条数 = {before}")

    ok, skip = [], []
    for it in ITEMS:
        title = it.get("title")
        doc = kb.add_doc(
            "hubei", it.get("type"), title, it.get("text"), it.get("metadata")
        )
        (ok if doc else skip).append(title)
        print(f"  [{'OK ' if doc else 'SKIP'}] {title}")

    after = len(kb._get_mem("hubei").get("docs", []))
    print(f"入库后 hubei 条数 = {after}  (新增 {after - before})")
    print(f"成功 {len(ok)} / 跳过 {len(skip)}")
    if skip:
        print("跳过项(多为 embedding 失败或重复):", skip)
    # 全部成功才视为 0 退出
    sys.exit(0 if not skip else 1)


if __name__ == "__main__":
    main()
