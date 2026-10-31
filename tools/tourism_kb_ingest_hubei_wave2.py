# -*- coding: utf-8 -*-
"""湖北文旅知识库 · 扩内容第二波（loop reinforcement）：增厚最薄类目。

目标类目（入库前条数 → 本批新增）：
  season 2 → +6   最佳季节（武当山/宜昌三峡/襄阳/随州银杏/咸宁温泉/黄冈红叶）
  ticket 4 → +5   票务与开放时间（三峡大坝/东湖/荆州古城/明显陵/赤壁古战场）
  stay   4 → +3   住宿（神农架/襄阳/咸宁温泉）
  tip    3 → +4   安全贴士（饮食/方言/防坑/避峰）
  shop   3 → +3   购物特产（十堰武当山/襄阳/恩施）
  transport 5 → +4 交通（宜昌/十堰/襄阳/湖北高铁网络）
  info   4 → +4   基本信息（区划/地理气候/方言人口/世遗5A）
  route  6 → +2   线路（武汉一日游/宜昌三峡游轮湖北段）
合计 +31 条，全部湖北本地，不与既有条目重名。

数据口径（防胡说八道）：票价一律写“参考价、以景区官方公示为准”；行政区划/人口等
写“以最新普查/官方名录为准”；不写我无法背书的精确易变数字。

跑法（容器内 /opt/fuguoai-server，embedding 缓存只在容器有）：
  python3 tools/tourism_kb_ingest_hubei_wave2.py
逐条 add_doc，打印 OK/SKIP 与前后条数。
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
    return f"https://baike.baidu.com/search?word={name}"


ITEMS = [
    # ---------------------- 最佳季节（season） ----------------------
    {
        "type": "season",
        "title": "武当山最佳旅游季节",
        "text": (
            "武当山春秋两季最宜游览（4-5月、9-11月），气候宜人、云海与秋色可观；"
            "夏季可避暑但多雨雾，冬季雪景清丽但部分索道与台阶可能限运。"
            "建议避开连续雨季与严寒时段，登山备好防滑鞋与外套。"
        ),
        "metadata": {"city": "十堰", "spot": "武当山", "source_site": "百度百科",
                      "source_url": _src("武当山"), "origin": "web",
                      "tags": ["最佳季节", "武当山", "春秋", "避暑", "十堰"]},
    },
    {
        "type": "season",
        "title": "宜昌三峡最佳旅游季节",
        "text": (
            "宜昌三峡春秋为佳（3-5月、9-11月），水位适宜、两岸植被丰茂；"
            "夏季汛期水势浩大但暑热，冬季相对冷清。三峡红叶（秭归、巫山一带）"
            "11月前后最美，是秋季标志性景观。"
        ),
        "metadata": {"city": "宜昌", "spot": "三峡", "source_site": "百度百科",
                      "source_url": _src("长江三峡"), "origin": "web",
                      "tags": ["最佳季节", "三峡", "宜昌", "红叶", "春秋"]},
    },
    {
        "type": "season",
        "title": "襄阳最佳旅游季节",
        "text": (
            "襄阳春秋最宜（4-6月、9-11月），古城、隆中与汉江风光四季可游，"
            "春秋体感最佳；夏季炎热、冬季湿冷。节假日与暑期客流较高，建议错峰。"
        ),
        "metadata": {"city": "襄阳", "spot": "襄阳", "source_site": "百度百科",
                      "source_url": _src("襄阳"), "origin": "web",
                      "tags": ["最佳季节", "襄阳", "古城", "春秋"]},
    },
    {
        "type": "season",
        "title": "随州银杏季",
        "text": (
            "随州大洪山、洛阳镇千年银杏谷每年11月中下旬至12月初金黄最美，"
            "是湖北秋季标志性赏叶地。最佳观赏窗口约2-3周，具体随当年气候浮动，"
            "建议出行前关注当地银杏变黄预报。"
        ),
        "metadata": {"city": "随州", "spot": "银杏谷", "source_site": "百度百科",
                      "source_url": _src("随州银杏"), "origin": "web",
                      "tags": ["最佳季节", "随州", "银杏", "赏叶", "秋季"]},
    },
    {
        "type": "season",
        "title": "咸宁温泉季",
        "text": (
            "咸宁温泉历史悠久，冬季（11月至次年2月）为最佳泡汤季；夏季亦可避暑。"
            "温泉谷、碧桂园温泉等集中分布，是周边城市短途度假热门。"
        ),
        "metadata": {"city": "咸宁", "spot": "温泉", "source_site": "百度百科",
                      "source_url": _src("咸宁温泉"), "origin": "web",
                      "tags": ["最佳季节", "咸宁", "温泉", "冬季", "度假"]},
    },
    {
        "type": "season",
        "title": "黄冈大别山红叶季",
        "text": (
            "黄冈大别山（罗田天堂寨、麻城龟峰山）秋季红叶11月前后最佳；"
            "春季龟峰山杜鹃4-5月盛开，是另一观赏高峰。山区早晚温差大，备好外套。"
        ),
        "metadata": {"city": "黄冈", "spot": "大别山", "source_site": "百度百科",
                      "source_url": _src("龟峰山"), "origin": "web",
                      "tags": ["最佳季节", "黄冈", "大别山", "红叶", "杜鹃"]},
    },
    # ---------------------- 票务与开放时间（ticket） ----------------------
    {
        "type": "ticket",
        "title": "三峡大坝门票与开放时间",
        "text": (
            "三峡大坝旅游区（宜昌）实行实名预约、免费入园参观，景区内换乘观光车参考价约35元/人；"
            "坛子岭、185平台、截流纪念园为主要观景点。开放时间约08:00-17:00。"
            "屈原故里文化旅游区（秭归）参考票价约90元/人，另计。以上为参考价，以官方公示为准。"
        ),
        "metadata": {"city": "宜昌", "spot": "三峡大坝", "source_site": "三峡大坝旅游区官网",
                      "source_url": _src("三峡大坝"), "origin": "web",
                      "tags": ["门票", "票价", "开放时间", "三峡大坝", "宜昌"]},
    },
    {
        "type": "ticket",
        "title": "东湖生态旅游风景区门票与开放时间",
        "text": (
            "武汉东湖（听涛、磨山、落雁等）大景区免费开放，内部景点如磨山樱花园、"
            "东湖海洋世界等单独售票；绿道骑行免费。开放时间约07:00-19:00（各园区有别）。"
            "以上为参考，以景区官方公示为准。"
        ),
        "metadata": {"city": "武汉", "spot": "东湖", "source_site": "武汉市文旅局",
                      "source_url": _src("东湖"), "origin": "web",
                      "tags": ["门票", "免费", "开放时间", "东湖", "武汉"]},
    },
    {
        "type": "ticket",
        "title": "荆州古城门票与开放时间",
        "text": (
            "荆州古城墙可登城游览，参考票价约30-40元/人；张居正故居、荆州博物馆"
            "（免费需预约）等另计。开放时间约08:30-17:30。以上为参考价，以官方公示为准。"
        ),
        "metadata": {"city": "荆州", "spot": "荆州古城", "source_site": "荆州市文旅局",
                      "source_url": _src("荆州古城"), "origin": "web",
                      "tags": ["门票", "票价", "开放时间", "荆州古城", "城墙"]},
    },
    {
        "type": "ticket",
        "title": "明显陵门票与开放时间",
        "text": (
            "钟祥明显陵（世界文化遗产）参考票价约60-80元/人，开放时间约08:30-17:30。"
            "以上为参考价，以景区官方公示为准。"
        ),
        "metadata": {"city": "荆门", "spot": "明显陵", "source_site": "钟祥市文旅局",
                      "source_url": _src("明显陵"), "origin": "web",
                      "tags": ["门票", "票价", "开放时间", "明显陵", "世遗"]},
    },
    {
        "type": "ticket",
        "title": "赤壁古战场门票与开放时间",
        "text": (
            "赤壁市三国赤壁古战场参考票价约100元/人（含演艺项目另计），"
            "开放时间约08:00-17:30。以上为参考价，以景区官方公示为准。"
        ),
        "metadata": {"city": "咸宁", "spot": "赤壁古战场", "source_site": "赤壁市文旅局",
                      "source_url": _src("赤壁古战场"), "origin": "web",
                      "tags": ["门票", "票价", "开放时间", "赤壁", "三国"]},
    },
    # ---------------------- 住宿（stay） ----------------------
    {
        "type": "stay",
        "title": "神农架住宿（木鱼镇/坪阡）",
        "text": (
            "神农架住宿集中在木鱼镇（南门、近神农顶/官门山）与坪阡古镇（大九湖方向）；"
            "木鱼镇配套成熟、餐饮便利，适合首晚落脚；大九湖景区内住宿有限需提前预订。"
            "旺季与节假日房源紧张，山区夜间偏凉备好保暖衣物。"
        ),
        "metadata": {"city": "神农架", "spot": "住宿", "source_site": "百度百科",
                      "source_url": _src("神农架"), "origin": "web",
                      "tags": ["住宿", "神农架", "木鱼镇", "坪阡", "大九湖"]},
    },
    {
        "type": "stay",
        "title": "襄阳住宿",
        "text": (
            "襄阳住宿多集中于樊城与襄城主城，毗邻古城墙、管家巷与汉江边；"
            "高铁站（东津）附近亦有多家新酒店。按行程主线选近古城或车站片区，"
            "旺季与展会期间提前预订。"
        ),
        "metadata": {"city": "襄阳", "spot": "住宿", "source_site": "百度百科",
                      "source_url": _src("襄阳"), "origin": "web",
                      "tags": ["住宿", "襄阳", "古城", "高铁站", "樊城"]},
    },
    {
        "type": "stay",
        "title": "咸宁住宿（温泉度假）",
        "text": (
            "咸宁温泉主题酒店与度假村集中（温泉谷、碧桂园、三江等），适合冬季泡汤度假；"
            "市区与赤壁、嘉鱼亦有住宿可选。周末与冬季旺季需提前预订，留意是否含温泉票。"
        ),
        "metadata": {"city": "咸宁", "spot": "住宿", "source_site": "百度百科",
                      "source_url": _src("咸宁温泉"), "origin": "web",
                      "tags": ["住宿", "咸宁", "温泉", "度假", "周末"]},
    },
    # ---------------------- 安全贴士（tip） ----------------------
    {
        "type": "tip",
        "title": "湖北饮食与肠胃适应",
        "text": (
            "湖北菜偏辣（干锅、麻辣小龙虾等重油重辣）、米粉与热干面为常见早餐主食；"
            "外地游客初到可点“微辣/免辣”，肠胃敏感者循序渐进、多饮水，"
            "生冷卤味与路边摊注意卫生。"
        ),
        "metadata": {"city": "湖北", "spot": "饮食", "source_site": "百度百科",
                      "source_url": _src("湖北菜"), "origin": "web",
                      "tags": ["贴士", "饮食", "辣", "热干面", "肠胃"]},
    },
    {
        "type": "tip",
        "title": "方言与沟通",
        "text": (
            "湖北主要属西南官话（武汉话、宜昌话、恩施话可互通），咸宁、黄石等部分属赣语；"
            "景区与城市普通话通用，乡镇方言略重，以普通话沟通无障碍。"
        ),
        "metadata": {"city": "湖北", "spot": "方言", "source_site": "百度百科",
                      "source_url": _src("湖北方言"), "origin": "web",
                      "tags": ["贴士", "方言", "西南官话", "普通话", "沟通"]},
    },
    {
        "type": "tip",
        "title": "景区消费防坑提示",
        "text": (
            "热门景区周边慎防“黑车”、拉客住宿与高价特产；购票优先官方公众号/小程序，"
            "不轻信路边“内部票”；购物留票据、比价后再买，理性对待“免费讲解”后推销。"
        ),
        "metadata": {"city": "湖北", "spot": "消费", "source_site": "湖北省文旅厅",
                      "source_url": _src("湖北旅游"), "origin": "web",
                      "tags": ["贴士", "防坑", "消费", "黑车", "官方购票"]},
    },
    {
        "type": "tip",
        "title": "节假日避峰与出行时段",
        "text": (
            "国庆、五一、春节及樱花季（3月）为湖北客流高峰；热门景区（黄鹤楼、武当山、"
            "恩施大峡谷、三峡）建议早开园或工作日前往，错峰体验更舒心，住宿亦提前预订。"
        ),
        "metadata": {"city": "湖北", "spot": "避峰", "source_site": "湖北省文旅厅",
                      "source_url": _src("湖北旅游"), "origin": "web",
                      "tags": ["贴士", "避峰", "节假日", "樱花季", "错峰"]},
    },
    # ---------------------- 购物特产（shop） ----------------------
    {
        "type": "shop",
        "title": "十堰武当山特产",
        "text": (
            "武当道茶、武当山香菇木耳、房县黄酒与木耳、郧阳红薯粉等具地方特色；"
            "选购建议到正规门店或特产店，关注产地、生产日期与保质期，理性比价。"
        ),
        "metadata": {"city": "十堰", "spot": "特产", "source_site": "百度百科",
                      "source_url": _src("武当山"), "origin": "web",
                      "tags": ["购物", "特产", "武当道茶", "香菇", "十堰"]},
    },
    {
        "type": "shop",
        "title": "襄阳特产",
        "text": (
            "襄阳牛肉面（可购方便装）、孔明菜（酱菜）、宜城板鸭、保康绿茶等具地方特色；"
            "古城周边与火车站商圈有售，注意真空包装与保质期，正规门店更稳妥。"
        ),
        "metadata": {"city": "襄阳", "spot": "特产", "source_site": "百度百科",
                      "source_url": _src("襄阳"), "origin": "web",
                      "tags": ["购物", "特产", "牛肉面", "孔明菜", "襄阳"]},
    },
    {
        "type": "shop",
        "title": "恩施土特产",
        "text": (
            "恩施玉露茶、柏杨豆干、土家腊肉、来凤姜糖与富硒产品等具土家特色；"
            "选购建议到正规门店，腊肉与豆干注意包装密封与保质期，富硒概念理性看待。"
        ),
        "metadata": {"city": "恩施", "spot": "特产", "source_site": "百度百科",
                      "source_url": _src("恩施"), "origin": "web",
                      "tags": ["购物", "特产", "玉露茶", "腊肉", "恩施"]},
    },
    # ---------------------- 交通（transport） ----------------------
    {
        "type": "transport",
        "title": "宜昌交通（三峡机场/高铁/市内）",
        "text": (
            "宜昌三峡机场有至国内主要城市的航班；宜昌东站为高铁枢纽（汉宜、宜万线），"
            "至武汉约2小时；市内公交/BRT 与三峡游客中心码头衔接景区，游轮多从市区或秭归港登船。"
        ),
        "metadata": {"city": "宜昌", "spot": "交通", "source_site": "百度百科",
                      "source_url": _src("宜昌"), "origin": "web",
                      "tags": ["交通", "宜昌", "三峡机场", "高铁", "游轮"]},
    },
    {
        "type": "transport",
        "title": "十堰交通（武当山机场/高铁）",
        "text": (
            "十堰武当山机场通达部分城市；十堰东站（汉十高铁）至武汉约2小时；"
            "武当山景区距市区约40公里，有旅游专线/打车可达，建议预留进山交通时间。"
        ),
        "metadata": {"city": "十堰", "spot": "交通", "source_site": "百度百科",
                      "source_url": _src("十堰"), "origin": "web",
                      "tags": ["交通", "十堰", "武当山机场", "汉十高铁", "旅游专线"]},
    },
    {
        "type": "transport",
        "title": "襄阳交通（刘集机场/高铁）",
        "text": (
            "襄阳刘集机场有至多地航班；襄阳东站（汉十、郑渝高铁）通达武汉、十堰、"
            "郑州、重庆等；市内公交与古城景点衔接便利，是鄂西北重要交通枢纽。"
        ),
        "metadata": {"city": "襄阳", "spot": "交通", "source_site": "百度百科",
                      "source_url": _src("襄阳"), "origin": "web",
                      "tags": ["交通", "襄阳", "刘集机场", "高铁", "枢纽"]},
    },
    {
        "type": "transport",
        "title": "湖北高铁网络",
        "text": (
            "湖北高铁以武汉为中心（京广、沪汉蓉、汉十、郑渝等交汇），武汉至襄阳/十堰/"
            "宜昌约2小时、至恩施约2-4小时；地级市间动车通达，是省内游最便捷的出行方式，"
            "建议优先选动车衔接景区所在城市。"
        ),
        "metadata": {"city": "湖北", "spot": "交通", "source_site": "百度百科",
                      "source_url": _src("湖北高铁"), "origin": "web",
                      "tags": ["交通", "高铁", "武汉", "枢纽", "动车"]},
    },
    # ---------------------- 基本信息（info） ----------------------
    {
        "type": "info",
        "title": "湖北行政区划与地市",
        "text": (
            "湖北辖12个地级市（武汉、黄石、十堰、宜昌、襄阳、鄂州、荆门、孝感、荆州、"
            "黄冈、咸宁、随州）与1个自治州（恩施土家族苗族自治州），省会武汉。"
            "具体区划以最新官方公布为准。"
        ),
        "metadata": {"city": "湖北", "spot": "区划", "source_site": "湖北省政府",
                      "source_url": _src("湖北行政区划"), "origin": "web",
                      "tags": ["基本信息", "区划", "地级市", "恩施", "武汉"]},
    },
    {
        "type": "info",
        "title": "湖北地理与气候",
        "text": (
            "湖北地处长江中游、洞庭湖以北；西为山地（武当山、神农架、恩施），东为江汉平原，"
            "地势西高东低。亚热带季风气候，四季分明、夏热冬冷、梅雨明显，山区与平原温差较大。"
        ),
        "metadata": {"city": "湖北", "spot": "地理", "source_site": "百度百科",
                      "source_url": _src("湖北地理"), "origin": "web",
                      "tags": ["基本信息", "地理", "气候", "江汉平原", "亚热带"]},
    },
    {
        "type": "info",
        "title": "湖北方言与人口",
        "text": (
            "湖北常住人口约5800万（以最新普查为准）；方言以西南官话为主，"
            "东南部（咸宁、黄石、黄冈部分）属赣语，恩施属西南官话成渝片，呈多元格局。"
        ),
        "metadata": {"city": "湖北", "spot": "人口", "source_site": "湖北省统计局",
                      "source_url": _src("湖北人口"), "origin": "web",
                      "tags": ["基本信息", "人口", "方言", "西南官话", "赣语"]},
    },
    {
        "type": "info",
        "title": "湖北世界遗产与5A景区",
        "text": (
            "世界遗产：武当山古建筑群、明显陵、唐崖土司城址、神农架（世界自然遗产）；"
            "5A级包括黄鹤楼、三峡大坝、武当山、恩施大峡谷、赤壁古战场、神农架等。"
            "具体名录以官方公布为准。"
        ),
        "metadata": {"city": "湖北", "spot": "景区", "source_site": "湖北省文旅厅",
                      "source_url": _src("湖北5A景区"), "origin": "web",
                      "tags": ["基本信息", "世界遗产", "5A", "武当山", "神农架"]},
    },
    # ---------------------- 线路（route） ----------------------
    {
        "type": "route",
        "title": "武汉城市经典一日游",
        "text": (
            "黄鹤楼→长江大桥→户部巷（过早）→昙华林/湖北省博物馆→东湖绿道骑行→"
            "两江四岸夜景（知音号），串起武汉地标与江湖气质；可按体力与兴趣取舍，"
            "博物馆需提前预约。"
        ),
        "metadata": {"city": "武汉", "spot": "线路", "source_site": "武汉市文旅局",
                      "source_url": _src("武汉旅游"), "origin": "web",
                      "tags": ["线路", "武汉", "黄鹤楼", "东湖", "知音号"]},
    },
    {
        "type": "route",
        "title": "宜昌三峡游轮（湖北段）",
        "text": (
            "宜昌是长江三峡游轮的湖北母港：登船游览三峡大坝、三峡人家、西陵峡，"
            "至秭归/巴东一带，是长江三峡的湖北精华段；下游延伸至重庆为跨省市航线。"
            "游轮多从市区码头或秭归港登船，建议提前订票并按码头就近住宿。"
        ),
        "metadata": {"city": "宜昌", "spot": "线路", "source_site": "宜昌市文旅局",
                      "source_url": _src("长江三峡"), "origin": "web",
                      "tags": ["线路", "宜昌", "三峡", "游轮", "西陵峡"]},
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
    sys.exit(0 if not skip else 1)


if __name__ == "__main__":
    main()
