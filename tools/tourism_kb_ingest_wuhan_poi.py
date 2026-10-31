# -*- coding: utf-8 -*-
"""武汉城市级 POI 补录（商业街 / 广场 / 公园 / 网红巷）。

数据来源：百度百科等公开权威资料（已核实具体数字，防胡说八道）。
跑法（容器内 /opt/fuguoai-server，embedding 模型缓存只在容器有）：
  python3 tools/tourism_kb_ingest_wuhan_poi.py
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


# 武汉城市级 POI（城市客厅 / 商业街 / 广场 / 公园 / 网红巷）。
# type 统一用 scenic（仓内无 park/plaza 专用类型，沿用江汉路步行街的存储约定）；
# metadata 沿用既有约定：city / spot / source_site / source_url / origin，并附带 tags 提升召回。
ITEMS = [
    {
        "type": "scenic",
        "title": "楚河汉街",
        "text": (
            "楚河汉街位于武汉中央文化区，是一条长约1.5公里、连接东湖与沙湖的商业步行街，"
            "2011年9月30日开放。街区以民国风情与现代风格结合，沿河设有屈原、李白等5座名人广场，"
            "云集杜莎夫人蜡像馆、汉秀剧场等文化地标，是武汉热门的购物休闲与网红打卡地。"
        ),
        "metadata": {
            "city": "武汉", "spot": "楚河汉街", "source_site": "百度百科",
            "source_url": _src("楚河汉街"), "origin": "web",
            "tags": ["商业街", "网红打卡", "中央文化区", "汉秀", "蜡像馆"],
        },
    },
    {
        "type": "scenic",
        "title": "昙华林",
        "text": (
            "昙华林地处武昌老城东北角，全长约1.2公里，距今已有600多年历史。街区深藏50余栋百年优秀历史建筑，"
            "是中国近代教育的诞生地之一、中国第一家公共图书馆落户之处。2021年10月改造后以人文小镇新貌迎客，"
            "文艺小店与历史建筑交织，是武汉著名的文艺网红街区。"
        ),
        "metadata": {
            "city": "武汉", "spot": "昙华林", "source_site": "百度百科",
            "source_url": _src("昙华林"), "origin": "web",
            "tags": ["历史街区", "网红巷", "文艺", "百年建筑", "武昌"],
        },
    },
    {
        "type": "scenic",
        "title": "中山公园",
        "text": (
            "武汉中山公园占地32.8万平方米，1910年始建（初名西园），是武汉历史最悠久的综合公园之一。"
            "园内有1945年武汉地区受降仪式原址受降堂、大型游乐场及丰富植被。作为城市中心绿地，"
            "中山公园集历史纪念、休闲游憩于一体，是市民与游客亲近自然的重要场所。"
        ),
        "metadata": {
            "city": "武汉", "spot": "中山公园", "source_site": "百度百科",
            "source_url": _src("中山公园"), "origin": "web",
            "tags": ["公园", "受降堂", "游乐场", "历史纪念", "武昌"],
        },
    },
    {
        "type": "scenic",
        "title": "洪山广场",
        "text": (
            "洪山广场位于武昌中北路，占地约10.8万平方米，1991年建成、2013年重新开放，"
            "是华中地区较大的城市广场之一。广场标志性景观为火凤凰雕塑，地下衔接地铁2号线与4号线洪山广场站。"
            "作为城市客厅，这里常举办公共活动，是市民休闲集散的核心空间。"
        ),
        "metadata": {
            "city": "武汉", "spot": "洪山广场", "source_site": "百度百科",
            "source_url": _src("洪山广场"), "origin": "web",
            "tags": ["广场", "火凤凰", "城市客厅", "地铁2号线", "地铁4号线"],
        },
    },
    {
        "type": "scenic",
        "title": "光谷步行街",
        "text": (
            "光谷步行街（世界城）位于武汉东湖高新区，全长1350米，占地609亩，汇聚约5176家商铺，"
            "年均客流量达1.5亿人次。街区融合罗马、西班牙等多国建筑风格，是光谷商圈的核心商业轴线，"
            "集购物、餐饮、娱乐于一体，也是年轻人与游客热衷的潮流打卡地。"
        ),
        "metadata": {
            "city": "武汉", "spot": "光谷步行街", "source_site": "百度百科",
            "source_url": _src("光谷步行街"), "origin": "web",
            "tags": ["商业街", "光谷", "东湖高新区", "购物", "潮流打卡"],
        },
    },
    {
        "type": "scenic",
        "title": "黎黄陂路",
        "text": (
            "黎黄陂路位于汉口江岸区，全长604米，1900年（清光绪二十六年）始建，两侧保留17处租界时期历史建筑，"
            "被称为“街头博物馆”。作为汉口历史文化风貌区核心，沿街咖啡馆、酒吧与画廊林立，"
            "红砖老建筑与时尚业态交融，是武汉最具腔调的文艺漫游街区。"
        ),
        "metadata": {
            "city": "武汉", "spot": "黎黄陂路", "source_site": "百度百科",
            "source_url": _src("黎黄陂路"), "origin": "web",
            "tags": ["历史街区", "网红巷", "汉口", "租界建筑", "咖啡馆"],
        },
    },
    {
        "type": "scenic",
        "title": "解放公园",
        "text": (
            "解放公园占地约46万平方米，1952年正式建园，是武汉大型综合性公园。园内有1956年迁建的"
            "苏联空军志愿队烈士墓，植有约400株樱花及大片草坪树林，并设人工湖与游乐设施。"
            "四季景致分明，春樱秋叶皆宜，是市民休闲、亲子游憩与缅怀先烈的城市绿地。"
        ),
        "metadata": {
            "city": "武汉", "spot": "解放公园", "source_site": "百度百科",
            "source_url": _src("解放公园"), "origin": "web",
            "tags": ["公园", "樱花", "苏联空军志愿队烈士墓", "亲子", "江岸"],
        },
    },
    {
        "type": "scenic",
        "title": "东湖绿道",
        "text": (
            "东湖绿道环抱武汉东湖，全长约105公里，由湖中道、湖山道、磨山道、郊野道、听涛道、白马道、森林道"
            "7条主题绿道组成，2024年三期工程贯通。绿道串联东湖生态景观，曾获中国建设工程鲁班奖，"
            "是市民骑行、跑步、亲水的城市生态廊道与休闲名片。"
        ),
        "metadata": {
            "city": "武汉", "spot": "东湖绿道", "source_site": "百度百科",
            "source_url": _src("东湖绿道"), "origin": "web",
            "tags": ["绿道", "公园", "东湖", "骑行", "鲁班奖", "生态"],
        },
    },
    {
        "type": "scenic",
        "title": "首义广场",
        "text": (
            "首义广场位于武昌阅马场，是纪念辛亥革命的标志性城市广场。2000年始建（原名首义文化园），"
            "2007年整合为5.42万平方米，2011年纪念辛亥革命100周年扩建至约21.95万平方米。"
            "以孙中山像至黄兴拜将台为中轴线，设十八星旗喷泉花坛、辛亥革命博物馆（红楼）、彭刘杨烈士雕像，全天免费开放。"
        ),
        "metadata": {
            "city": "武汉", "spot": "首义广场", "source_site": "百度百科",
            "source_url": _src("首义广场"), "origin": "web",
            "tags": ["广场", "辛亥革命", "红楼", "孙中山像", "黄兴拜将台", "免费"],
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
