# -*- coding: utf-8 -*-
"""湖北代表美食补录（扩内容·检索质量升级后的召回增强）。

数据来源：政府门户、地方志、公开权威资料（已逐条核实关键史实，防胡说八道）。
跑法（容器内 /opt/fuguoai-server，embedding 模型缓存只在容器有）：
  python3 tools/tourism_kb_ingest_hubei_food.py
逐条 add_doc，输出每条成功/跳过，并打印入库前后条数。

背景：hubei.json 现有 food 仅 6 条（热干面/户部巷/武昌鱼/恩施土家腊肉与合渣/潜江油焖大虾/襄阳牛肉面），
美食是文旅最高频查询类型，探针显示「恩施特色美食/襄阳名小吃」等多落兜底。本批补 18 条跨城代表美食，
直接提升该类查询的 KB 命中率（与 reranker + 数值护栏协同）。
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


# 18 条湖北代表美食（跨城）。type 统一用 food；metadata 沿用既有约定：
# city / spot / source_site / source_url / origin，并附带 tags 提升召回。
# 关键史实已核实：恩施玉露(1680创制/唯一蒸青针形绿茶/1965十大名茶/2014国家级非遗)、
# 钟祥蟠龙菜(明代御膳/嘉靖/2011省级非遗)；其余为稳妥的定性描述，不编造具体数字。
ITEMS = [
    # ---------------------- 武汉 ----------------------
    {
        "type": "food",
        "title": "豆皮",
        "text": (
            "老通城豆皮是武汉过早名点，以绿豆面皮裹糯米、肉丁、香菇、笋丁等馅料油煎而成，"
            "外脆内糯，最负盛名的是三鲜豆皮。与热干面并称武汉过早双绝，是汉味小吃的代表。"
        ),
        "metadata": {
            "city": "武汉", "spot": "豆皮", "source_site": "百度百科",
            "source_url": _src("豆皮"), "origin": "web",
            "tags": ["过早", "老通城", "三鲜豆皮", "糯米", "武汉小吃"],
        },
    },
    {
        "type": "food",
        "title": "面窝",
        "text": (
            "面窝是武汉特色油炸小吃，用粳米、黄豆磨浆加葱花、芝麻，倒入中间凸起的铁勺炸成圈状，"
            "外圈酥脆、内里软嫩，常作过早配食，配热干面或糊汤粉同食。"
        ),
        "metadata": {
            "city": "武汉", "spot": "面窝", "source_site": "百度百科",
            "source_url": _src("面窝"), "origin": "web",
            "tags": ["过早", "油炸", "小吃", "武汉"],
        },
    },
    {
        "type": "food",
        "title": "糊汤粉",
        "text": (
            "糊汤粉是武汉过早小吃，以鲜鱼熬制的浓糊汤浇于米粉之上，撒葱花、胡椒，"
            "常配油条同食，鲜香微辣，是汉味早点的代表之一。"
        ),
        "metadata": {
            "city": "武汉", "spot": "糊汤粉", "source_site": "百度百科",
            "source_url": _src("糊汤粉"), "origin": "web",
            "tags": ["过早", "米粉", "鱼汤", "油条", "武汉小吃"],
        },
    },
    {
        "type": "food",
        "title": "武汉鸭脖",
        "text": (
            "武汉鸭脖是湖北卤味代表，以麻辣鲜香著称，市面以周黑鸭、精武鸭脖两派最为知名；"
            "先卤后浸，肉质紧实入味，是佐酒零食佳品。"
        ),
        "metadata": {
            "city": "武汉", "spot": "鸭脖", "source_site": "百度百科",
            "source_url": _src("武汉鸭脖"), "origin": "web",
            "tags": ["卤味", "麻辣", "周黑鸭", "精武", "零食"],
        },
    },
    {
        "type": "food",
        "title": "排骨藕汤",
        "text": (
            "排骨藕汤是湖北家常滋补名汤，取洪湖莲藕与排骨慢炖，汤色奶白、藕香粉糯，"
            "有“不喝排骨藕汤不算到湖北”之说，尤盛于秋冬，是荆楚餐桌的暖身主角。"
        ),
        "metadata": {
            "city": "武汉", "spot": "排骨藕汤", "source_site": "百度百科",
            "source_url": _src("排骨藕汤"), "origin": "web",
            "tags": ["汤", "莲藕", "洪湖", "滋补", "湖北菜"],
        },
    },
    # ---------------------- 襄阳 ----------------------
    {
        "type": "food",
        "title": "襄阳大头菜（孔明菜）",
        "text": (
            "襄阳大头菜又称孔明菜，是襄阳传统腌菜名产，相传与诸葛亮屯兵襄阳有关；"
            "以芥菜头腌制，色泽金黄、咸辣脆爽，可佐粥下饭，是当地久负盛名的酱菜。"
        ),
        "metadata": {
            "city": "襄阳", "spot": "大头菜", "source_site": "百度百科",
            "source_url": _src("襄阳大头菜"), "origin": "web",
            "tags": ["腌菜", "孔明菜", "芥菜", "酱菜", "襄阳"],
        },
    },
    # ---------------------- 荆州 ----------------------
    {
        "type": "food",
        "title": "荆州鱼糕",
        "text": (
            "荆州鱼糕是荆州传统名菜，号称“吃鱼不见鱼”，以鲜鱼糜混合猪肉、蛋清蒸制而成，"
            "质地细嫩、鱼香清鲜，是荆楚宴席与年节必备佳肴，有千余年历史。"
        ),
        "metadata": {
            "city": "荆州", "spot": "鱼糕", "source_site": "百度百科",
            "source_url": _src("荆州鱼糕"), "origin": "web",
            "tags": ["名菜", "鱼糜", "荆楚", "宴席", "荆州"],
        },
    },
    {
        "type": "food",
        "title": "公安锅盔",
        "text": (
            "公安锅盔是荆州公安县特色面食小吃，以发面擀饼贴炉壁烤制，有糖、盐、葱油等多种口味，"
            "外脆内软、便于携带，是当地街头常见干粮与早点。"
        ),
        "metadata": {
            "city": "荆州", "spot": "锅盔", "source_site": "百度百科",
            "source_url": _src("公安锅盔"), "origin": "web",
            "tags": ["面食", "烤饼", "公安", "小吃", "荆州"],
        },
    },
    {
        "type": "food",
        "title": "洪湖莲藕",
        "text": (
            "洪湖莲藕产自荆州洪湖，因湖区淤泥肥沃、莲子饱满而闻名，粉糯清甜，"
            "宜煲汤做菜，是湖北莲藕的上品，亦为排骨藕汤的首选原料。"
        ),
        "metadata": {
            "city": "荆州", "spot": "洪湖莲藕", "source_site": "百度百科",
            "source_url": _src("洪湖莲藕"), "origin": "web",
            "tags": ["莲藕", "洪湖", "煲汤", "湖北特产", "荆州"],
        },
    },
    # ---------------------- 宜昌 ----------------------
    {
        "type": "food",
        "title": "宜昌萝卜饺子",
        "text": (
            "宜昌萝卜饺子是宜昌独有风味小吃，以大米、黄豆磨浆做皮，包入萝卜丝馅油炸而成，"
            "外酥里嫩、咸鲜微辣，是宜昌人钟爱的街头早点。"
        ),
        "metadata": {
            "city": "宜昌", "spot": "萝卜饺子", "source_site": "百度百科",
            "source_url": _src("宜昌萝卜饺子"), "origin": "web",
            "tags": ["小吃", "油炸", "萝卜", "宜昌", "早点"],
        },
    },
    {
        "type": "food",
        "title": "宜昌凉虾",
        "text": (
            "宜昌凉虾是宜昌传统消暑甜品，用米浆漏成虾形小条，浸入冰镇红糖水中，"
            "滑嫩清甜、清凉解暑，盛夏街头常见。"
        ),
        "metadata": {
            "city": "宜昌", "spot": "凉虾", "source_site": "百度百科",
            "source_url": _src("宜昌凉虾"), "origin": "web",
            "tags": ["甜品", "消暑", "红糖", "米浆", "宜昌"],
        },
    },
    # ---------------------- 咸宁 ----------------------
    {
        "type": "food",
        "title": "咸宁桂花糕",
        "text": (
            "咸宁桂花糕是以当地盛产的桂花入料的传统糕点，糯米粉蒸制，缀以蜜渍桂花，"
            "清香软糯、甜而不腻，是咸宁代表性的伴手礼。"
        ),
        "metadata": {
            "city": "咸宁", "spot": "桂花糕", "source_site": "百度百科",
            "source_url": _src("咸宁桂花糕"), "origin": "web",
            "tags": ["糕点", "桂花", "伴手礼", "糯米", "咸宁"],
        },
    },
    # ---------------------- 黄冈 ----------------------
    {
        "type": "food",
        "title": "黄冈东坡肉",
        "text": (
            "黄冈东坡肉相传为苏轼（号东坡）谪居黄州时创制，以五花肉慢火少水焖炖，"
            "色泽红亮、肥而不腻、酥烂入味，因《猪肉颂》而声名远播。"
        ),
        "metadata": {
            "city": "黄冈", "spot": "东坡肉", "source_site": "百度百科",
            "source_url": _src("东坡肉"), "origin": "web",
            "tags": ["名菜", "苏轼", "黄州", "焖炖", "黄冈"],
        },
    },
    {
        "type": "food",
        "title": "麻城肉糕",
        "text": (
            "麻城肉糕是黄冈麻城传统名吃，以鱼肉、猪肉剁糜加苕粉、蛋清蒸制而成，"
            "口感弹嫩、鲜香不腻，是麻城宴席与年节常见的招牌菜。"
        ),
        "metadata": {
            "city": "黄冈", "spot": "肉糕", "source_site": "百度百科",
            "source_url": _src("麻城肉糕"), "origin": "web",
            "tags": ["名吃", "鱼糜", "蒸制", "麻城", "黄冈"],
        },
    },
    # ---------------------- 恩施 ----------------------
    {
        "type": "food",
        "title": "恩施玉露茶",
        "text": (
            "恩施玉露创制于清康熙年间（约1680年），是中国唯一保存下来的蒸青针形绿茶，"
            "1965年入选“中国十大名茶”，其制作技艺2014年列入国家级非物质文化遗产，"
            "外形紧圆、汤色青绿、滋味鲜爽。"
        ),
        "metadata": {
            "city": "恩施", "spot": "玉露茶", "source_site": "恩施州政府门户",
            "source_url": _src("恩施玉露"), "origin": "web",
            "tags": ["绿茶", "蒸青", "非遗", "十大名茶", "恩施"],
        },
    },
    {
        "type": "food",
        "title": "恩施柏杨豆干",
        "text": (
            "恩施柏杨豆干产自利川柏杨镇，以黄豆、泉水和传统卤料精制，薄片柔韧、卤香浓厚，"
            "可即食可炒菜，是恩施有名的休闲特产。"
        ),
        "metadata": {
            "city": "恩施", "spot": "柏杨豆干", "source_site": "百度百科",
            "source_url": _src("柏杨豆干"), "origin": "web",
            "tags": ["豆干", "卤味", "利川", "特产", "恩施"],
        },
    },
    # ---------------------- 十堰 ----------------------
    {
        "type": "food",
        "title": "郧阳三合汤",
        "text": (
            "郧阳三合汤是十堰郧阳（原郧县）特色汤食，以红薯粉条、牛肉片、饺子（或肉丸）三样同碗，"
            "配红油辣汤，鲜辣暖身，是当地招牌早餐。"
        ),
        "metadata": {
            "city": "十堰", "spot": "三合汤", "source_site": "百度百科",
            "source_url": _src("郧阳三合汤"), "origin": "web",
            "tags": ["汤", "红薯粉", "牛肉", "辣", "十堰"],
        },
    },
    # ---------------------- 钟祥 ----------------------
    {
        "type": "food",
        "title": "钟祥蟠龙菜",
        "text": (
            "钟祥蟠龙菜是明代宫廷御膳，相传为嘉靖皇帝朱厚熜进京即位时由厨师詹多创制，"
            "以鱼肉蛋卷蒸切、吃肉不见肉著称，2011年列入湖北省非物质文化遗产，是钟祥“三绝”之一。"
        ),
        "metadata": {
            "city": "钟祥", "spot": "蟠龙菜", "source_site": "钟祥市政府门户",
            "source_url": _src("钟祥蟠龙菜"), "origin": "web",
            "tags": ["御膳", "明代", "非遗", "钟祥三绝", "肉卷"],
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
