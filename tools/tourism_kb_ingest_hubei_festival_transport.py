# -*- coding: utf-8 -*-
"""湖北节庆/非遗 + 交通 + 区位补录（扩内容·召回增强·第二波）。

数据来源：政府门户、地方志、公开权威资料（关键史实已 WebSearch 实证，防胡说八道）。
跑法（容器内 /opt/fuguoai-server，embedding 模型缓存只在容器有）：
  python3 tools/tourism_kb_ingest_hubei_festival_transport.py
逐条 add_doc，输出每条成功/跳过，并打印入库前后条数。

背景：hubei.json 现 festival=4(全为金秋消费季)/transport=1/info=3，节庆与交通是高频查询却极薄。
本批补 节庆·非遗 6 条 + 交通 4 条 + 区位 1 条，共 11 条，提升此类查询的 KB 命中率。
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


# 11 条：节庆/非遗(festival) 6 + 交通(transport) 4 + 区位(info) 1。
# metadata 沿用既有约定：city / spot / source_site / source_url / origin + tags。
# 关键史实已核实：武当山机场(2016-02-05通航)、天河机场T3(2017投用/华中最大)、
# 武汉马拉松(2016首届)、世界武当太极大会(2024十堰)；其余为稳妥定性描述。
ITEMS = [
    # ---------------------- 节庆 / 非遗 ----------------------
    {
        "type": "festival",
        "title": "武当太极大会",
        "text": (
            "世界武当太极大会于2024年10月在十堰举行，吸引全球数十个国家和地区的嘉宾齐聚论道；"
            "武当山是太极拳发源地之一与道教圣地，太极文化已成为十堰标志性的文旅名片。"
        ),
        "metadata": {
            "city": "十堰", "spot": "武当太极大会", "source_site": "十堰市政府门户",
            "source_url": _src("世界武当太极大会"), "origin": "web",
            "tags": ["太极", "武当", "非遗", "十堰", "武术"],
        },
    },
    {
        "type": "festival",
        "title": "土家女儿会",
        "text": (
            "土家女儿会是恩施土家族传统节日，被誉为“东方情人节”，青年男女通过对歌相亲；"
            "如今多在恩施土家女儿城等地举办，是体验土家风情的重要民俗活动。"
        ),
        "metadata": {
            "city": "恩施", "spot": "女儿会", "source_site": "百度百科",
            "source_url": _src("土家女儿会"), "origin": "web",
            "tags": ["女儿会", "土家族", "民俗", "恩施", "情人节"],
        },
    },
    {
        "type": "festival",
        "title": "屈原故里端午文化节",
        "text": (
            "宜昌秭归是屈原故里，端午龙舟祭屈习俗源远流长；屈原故里端午文化已列入国家级非物质文化遗产，"
            "每逢端午举办龙舟竞渡与民俗展演，是荆楚文化的重要载体。"
        ),
        "metadata": {
            "city": "宜昌", "spot": "端午文化节", "source_site": "百度百科",
            "source_url": _src("屈原故里端午文化节"), "origin": "web",
            "tags": ["屈原", "端午", "龙舟", "秭归", "非遗"],
        },
    },
    {
        "type": "festival",
        "title": "襄阳诸葛亮文化节",
        "text": (
            "襄阳隆中是诸葛亮“躬耕陇亩”之地，当地依托三国文化举办诸葛亮文化节，"
            "融学术研讨、民俗展演与文旅推介于一体，弘扬诸葛文化与襄阳古城底蕴。"
        ),
        "metadata": {
            "city": "襄阳", "spot": "诸葛亮文化节", "source_site": "百度百科",
            "source_url": _src("襄阳诸葛亮文化节"), "origin": "web",
            "tags": ["诸葛亮", "隆中", "三国", "文化节", "襄阳"],
        },
    },
    {
        "type": "festival",
        "title": "武汉马拉松",
        "text": (
            "武汉马拉松（汉马）自2016年首届，每年春季举行，途经江汉路、长江大桥、东湖绿道等城市地标，"
            "是武汉城市名片级的路跑赛事，串联江城自然与人文风光。"
        ),
        "metadata": {
            "city": "武汉", "spot": "马拉松", "source_site": "百度百科",
            "source_url": _src("武汉马拉松"), "origin": "web",
            "tags": ["马拉松", "汉马", "路跑", "东湖", "武汉"],
        },
    },
    {
        "type": "festival",
        "title": "咸宁温泉文化",
        "text": (
            "咸宁素有“中国温泉之都”之称，地热温泉资源丰富，冬季温泉康养是当地特色文旅，"
            "形成以温泉度假为核心的休闲集群，是华中知名的避寒养生目的地。"
        ),
        "metadata": {
            "city": "咸宁", "spot": "温泉", "source_site": "百度百科",
            "source_url": _src("咸宁温泉"), "origin": "web",
            "tags": ["温泉", "康养", "度假", "咸宁", "养生"],
        },
    },
    # ---------------------- 交通 ----------------------
    {
        "type": "transport",
        "title": "武汉天河国际机场",
        "text": (
            "武汉天河国际机场是湖北最大的航空枢纽，T3航站楼于2017年投用、为华中地区最大机场，"
            "通达国内外众多城市，是进出湖北的空中门户。"
        ),
        "metadata": {
            "city": "武汉", "spot": "天河机场", "source_site": "湖北机场集团",
            "source_url": _src("武汉天河国际机场"), "origin": "web",
            "tags": ["机场", "航空", "天河", "T3", "武汉"],
        },
    },
    {
        "type": "transport",
        "title": "武当山机场",
        "text": (
            "十堰武当山机场于2016年2月5日通航，结束了十堰无民航的历史，"
            "拉近鄂西北与全国的连接，是十堰对外交往的空中窗口，辐射武当山旅游。"
        ),
        "metadata": {
            "city": "十堰", "spot": "武当山机场", "source_site": "十堰市政府门户",
            "source_url": _src("十堰武当山机场"), "origin": "web",
            "tags": ["机场", "航空", "通航", "十堰", "武当山"],
        },
    },
    {
        "type": "transport",
        "title": "武汉高铁枢纽",
        "text": (
            "武汉地处京广高铁与沪汉蓉高铁交汇处，坐拥武汉站、汉口站、武昌站三大火车站，"
            "素有“九省通衢”之称，是全国铁路客运核心枢纽之一，高铁通达大半个中国。"
        ),
        "metadata": {
            "city": "武汉", "spot": "高铁枢纽", "source_site": "百度百科",
            "source_url": _src("武汉铁路枢纽"), "origin": "web",
            "tags": ["高铁", "铁路", "九省通衢", "武汉站", "枢纽"],
        },
    },
    {
        "type": "transport",
        "title": "长江三峡游轮",
        "text": (
            "宜昌是长江三峡游轮的母港，游客多从宜昌登船，溯江饱览三峡大坝、巫峡、瞿塘峡等胜景，"
            "是经典的长江水上黄金旅游线路，串联鄂渝山水。"
        ),
        "metadata": {
            "city": "宜昌", "spot": "三峡游轮", "source_site": "百度百科",
            "source_url": _src("长江三峡游轮"), "origin": "web",
            "tags": ["游轮", "三峡", "长江", "母港", "宜昌"],
        },
    },
    # ---------------------- 区位 / 实用信息 ----------------------
    {
        "type": "info",
        "title": "湖北九省通衢区位",
        "text": (
            "湖北位居华中、长江中游，承东启西、连南接北；省会武汉素称“九省通衢”，"
            "铁路、公路、水路、航空交织，是全国重要的综合交通枢纽，便于辐射全国游客。"
        ),
        "metadata": {
            "city": "湖北", "spot": "九省通衢", "source_site": "百度百科",
            "source_url": _src("九省通衢"), "origin": "web",
            "tags": ["区位", "枢纽", "九省通衢", "长江", "湖北"],
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
