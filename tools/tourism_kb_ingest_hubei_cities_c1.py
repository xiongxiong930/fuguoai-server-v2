# -*- coding: utf-8 -*-
"""湖北文旅知识库 · 自进化 C1 波：补齐地理空白地市。

扫描发现（2026-09-26，210 条）：黄石/鄂州/孝感 各 0 条 practical，随州/荆门 各 ≤1 条，
武汉/恩施/十堰 占大头。本波为每个空白地市补 scenic/food/stay/transport/season 基础条目，
全部湖北本地，不与既有 210 条重名。合计约 40 条。

数据口径（防胡说八道）：票价/具体数字一律写“参考价、以官方公示为准”；不写我无法背书的精确易变数。
机场/高铁等事实性信息（如鄂州花湖机场 2022 投运、武汉地铁11号线延伸至葛店）为公开事实。

跑法（容器内 /opt/fuguoai-server，embedding 缓存只在容器有）：
  python3 tools/tourism_kb_ingest_hubei_cities_c1.py
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
    # ===================== 黄石 =====================
    {
        "type": "scenic",
        "title": "黄石国家矿山公园",
        "text": (
            "黄石国家矿山公园位于黄石铁山区，是在废弃露天采矿场基础上建成的工业遗产主题公园，"
            "拥有被誉为“亚洲第一天坑”的矿坑景观，见证黄石从矿城到生态园的转型，是矿冶文化研学与打卡地。"
        ),
        "metadata": {"city": "黄石", "spot": "黄石国家矿山公园", "source_site": "百度百科",
                      "source_url": _src("黄石国家矿山公园"), "origin": "web",
                      "tags": ["黄石", "矿山公园", "工业遗产", "天坑", "矿冶文化"]},
    },
    {
        "type": "scenic",
        "title": "仙岛湖",
        "text": (
            "仙岛湖位于黄石阳新县，因湖中 1002 座岛屿星罗棋布而有“荆楚第一奇湖”之称，"
            "可乘船游岛、观湿地候鸟，是武汉城市圈周边的生态湖岛度假地。"
        ),
        "metadata": {"city": "黄石", "spot": "仙岛湖", "source_site": "百度百科",
                      "source_url": _src("仙岛湖"), "origin": "web",
                      "tags": ["黄石", "仙岛湖", "阳新", "岛屿", "生态"]},
    },
    {
        "type": "scenic",
        "title": "西塞山",
        "text": (
            "西塞山位于黄石市东郊长江边，地势险要、为古战场要塞，因唐代张志和“西塞山前白鹭飞”一词闻名；"
            "山有龙窟寺、北望亭等遗迹，可临江怀古。"
        ),
        "metadata": {"city": "黄石", "spot": "西塞山", "source_site": "百度百科",
                      "source_url": _src("西塞山"), "origin": "web",
                      "tags": ["黄石", "西塞山", "长江", "古战场", "张志和"]},
    },
    {
        "type": "scenic",
        "title": "磁湖",
        "text": (
            "磁湖是黄石市区内的天然湖泊，环湖有澄月岛、鲇鱼墩等景点，是市民休闲健身带，"
            "晨练与夜景皆宜，也是城市绿心。"
        ),
        "metadata": {"city": "黄石", "spot": "磁湖", "source_site": "百度百科",
                      "source_url": _src("磁湖"), "origin": "web",
                      "tags": ["黄石", "磁湖", "市区", "休闲", "湖泊"]},
    },
    {
        "type": "food",
        "title": "黄石港饼",
        "text": (
            "黄石港饼是黄石传统名点，芝麻糖皮、外酥内软，是当地代表性的茶点与伴手礼，"
            "历史悠久、老少皆宜。"
        ),
        "metadata": {"city": "黄石", "spot": "港饼", "source_site": "百度百科",
                      "source_url": _src("黄石港饼"), "origin": "web",
                      "tags": ["黄石", "港饼", "小吃", "伴手礼"]},
    },
    {
        "type": "food",
        "title": "阳新屯鸟与水产品",
        "text": (
            "阳新屯鸟（本地读音 tún niǎo，即番鸭）肉质紧实、为地方特色禽肉；保安湖、网湖等水域水产丰富，"
            "湖鲜值得一试。食用注意熟透、来源卫生。"
        ),
        "metadata": {"city": "黄石", "spot": "阳新屯鸟", "source_site": "百度百科",
                      "source_url": _src("阳新屯鸟"), "origin": "web",
                      "tags": ["黄石", "阳新", "屯鸟", "番鸭", "湖鲜"]},
    },
    {
        "type": "stay",
        "title": "黄石住宿",
        "text": (
            "黄石住宿多集中于黄石港区与磁湖周边；游览矿山公园、仙岛湖可就近住铁山区或阳新县。"
            "黄石在武汉城市圈内，也常作为武汉周边游的中转停留。"
        ),
        "metadata": {"city": "黄石", "spot": "住宿", "source_site": "百度百科",
                      "source_url": _src("黄石"), "origin": "web",
                      "tags": ["黄石", "住宿", "磁湖", "武汉城市圈"]},
    },
    {
        "type": "transport",
        "title": "黄石交通",
        "text": (
            "黄石北站（高铁）、黄石站通达武汉（约40分钟）及南昌等方向；武黄、大广高速与黄石长江大桥联通两岸；"
            "距武汉天河机场约1.5小时车程，自驾与高铁均便利。"
        ),
        "metadata": {"city": "黄石", "spot": "交通", "source_site": "百度百科",
                      "source_url": _src("黄石"), "origin": "web",
                      "tags": ["黄石", "交通", "高铁", "黄石北站", "武汉城市圈"]},
    },
    {
        "type": "season",
        "title": "黄石最佳旅游季节",
        "text": (
            "黄石春秋最宜（3-5月、9-11月），磁湖与仙岛湖水量充沛、气候舒适；"
            "夏季江边偏热、冬季湿冷，山区景点注意防寒。"
        ),
        "metadata": {"city": "黄石", "spot": "最佳季节", "source_site": "百度百科",
                      "source_url": _src("黄石"), "origin": "web",
                      "tags": ["黄石", "最佳季节", "春秋", "磁湖", "仙岛湖"]},
    },
    # ===================== 鄂州 =====================
    {
        "type": "scenic",
        "title": "梁子湖",
        "text": (
            "梁子湖是湖北第二大湖、武汉重要水源地，梁子岛可登岛游湖、品湖鲜，"
            "以生态观光与湖鲜美食为特色，是武汉周边热门的亲水去处。"
        ),
        "metadata": {"city": "鄂州", "spot": "梁子湖", "source_site": "百度百科",
                      "source_url": _src("梁子湖"), "origin": "web",
                      "tags": ["鄂州", "梁子湖", "梁子岛", "湖泊", "生态"]},
    },
    {
        "type": "scenic",
        "title": "鄂州西山",
        "text": (
            "鄂州西山临江而立，有吴王孙权避暑宫、武昌门等三国遗迹，登顶可远眺长江，"
            "是兼具三国文化与城市观江的近郊山地。"
        ),
        "metadata": {"city": "鄂州", "spot": "西山", "source_site": "百度百科",
                      "source_url": _src("鄂州西山"), "origin": "web",
                      "tags": ["鄂州", "西山", "三国", "孙权", "长江"]},
    },
    {
        "type": "scenic",
        "title": "莲花山",
        "text": (
            "莲花山位于鄂州城郊，以元明塔、碑林与园林景观著称，是礼佛、书法文化与休闲游览的去处。"
        ),
        "metadata": {"city": "鄂州", "spot": "莲花山", "source_site": "百度百科",
                      "source_url": _src("鄂州莲花山"), "origin": "web",
                      "tags": ["鄂州", "莲花山", "元明塔", "碑林", "园林"]},
    },
    {
        "type": "food",
        "title": "鄂州梁子湖大闸蟹与武昌鱼",
        "text": (
            "梁子湖是武昌鱼的重要产地之一，秋蟹肥美、武昌鱼清蒸为经典鄂菜；"
            "湖区湖鲜宴是当地代表性吃法，注意时令与鲜活度。"
        ),
        "metadata": {"city": "鄂州", "spot": "湖鲜", "source_site": "百度百科",
                      "source_url": _src("武昌鱼"), "origin": "web",
                      "tags": ["鄂州", "梁子湖", "大闸蟹", "武昌鱼", "湖鲜"]},
    },
    {
        "type": "stay",
        "title": "鄂州住宿",
        "text": (
            "鄂州住宿以鄂城区（市区）酒店最便利；近梁子湖可住梁子岛或梧桐湖新区。"
            "鄂州属武汉城市圈，多作武汉周边一日或两日游。"
        ),
        "metadata": {"city": "鄂州", "spot": "住宿", "source_site": "百度百科",
                      "source_url": _src("鄂州"), "origin": "web",
                      "tags": ["鄂州", "住宿", "鄂城", "梁子湖", "武汉城市圈"]},
    },
    {
        "type": "transport",
        "title": "鄂州交通（含花湖机场）",
        "text": (
            "鄂州站、鄂州东站通达高铁；鄂州花湖机场是亚洲首个专业货运枢纽机场（2022年投运），"
            "客运多以武汉天河机场为主。武汉地铁11号线已延伸至鄂州葛店，城际通勤便利。"
        ),
        "metadata": {"city": "鄂州", "spot": "交通", "source_site": "百度百科",
                      "source_url": _src("鄂州花湖机场"), "origin": "web",
                      "tags": ["鄂州", "交通", "花湖机场", "高铁", "武汉地铁"]},
    },
    {
        "type": "season",
        "title": "鄂州最佳旅游季节",
        "text": (
            "鄂州春秋为佳（3-5月、9-11月），梁子湖水位与气候宜人；"
            "夏季可避暑游湖、冬季偏冷，春秋体感最舒适。"
        ),
        "metadata": {"city": "鄂州", "spot": "最佳季节", "source_site": "百度百科",
                      "source_url": _src("鄂州"), "origin": "web",
                      "tags": ["鄂州", "最佳季节", "春秋", "梁子湖"]},
    },
    # ===================== 孝感 =====================
    {
        "type": "scenic",
        "title": "双峰山",
        "text": (
            "双峰山位于孝感（孝昌/大悟间），是孝感最高峰，森林覆盖、峡谷溪流清澈，"
            "是登山、避暑与亲子户外的去处。"
        ),
        "metadata": {"city": "孝感", "spot": "双峰山", "source_site": "百度百科",
                      "source_url": _src("双峰山"), "origin": "web",
                      "tags": ["孝感", "双峰山", "登山", "避暑", "森林"]},
    },
    {
        "type": "scenic",
        "title": "安陆白兆山（李白故里）",
        "text": (
            "白兆山位于安陆市，唐代诗人李白曾“酒隐安陆”十年，山有李白读书台、太白崖等遗迹，"
            "是文化登山与唐诗寻迹的胜地。"
        ),
        "metadata": {"city": "孝感", "spot": "白兆山", "source_site": "百度百科",
                      "source_url": _src("白兆山"), "origin": "web",
                      "tags": ["孝感", "安陆", "白兆山", "李白", "文化"]},
    },
    {
        "type": "scenic",
        "title": "汤池温泉",
        "text": (
            "汤池温泉位于应城市，历史悠久的温泉度假地，冬季泡汤热门；周边农业观光与乡村游亦较成熟。"
        ),
        "metadata": {"city": "孝感", "spot": "汤池温泉", "source_site": "百度百科",
                      "source_url": _src("汤池温泉"), "origin": "web",
                      "tags": ["孝感", "应城", "汤池", "温泉", "度假"]},
    },
    {
        "type": "food",
        "title": "孝感米酒与麻糖",
        "text": (
            "孝感糊汤米酒甜润香醇、米酒糍粑知名；孝感麻糖酥香松脆，是湖北经典伴手礼，"
            "选购注意正规包装与保质期。"
        ),
        "metadata": {"city": "孝感", "spot": "米酒麻糖", "source_site": "百度百科",
                      "source_url": _src("孝感米酒"), "origin": "web",
                      "tags": ["孝感", "米酒", "麻糖", "伴手礼", "小吃"]},
    },
    {
        "type": "food",
        "title": "安陆白花菜与翰林鸡",
        "text": (
            "安陆白花菜为地方特色腌菜、风味独特；翰林鸡为安陆传统名菜，是孝感地方菜的代表之一。"
        ),
        "metadata": {"city": "孝感", "spot": "白花菜翰林鸡", "source_site": "百度百科",
                      "source_url": _src("安陆"), "origin": "web",
                      "tags": ["孝感", "安陆", "白花菜", "翰林鸡", "地方菜"]},
    },
    {
        "type": "stay",
        "title": "孝感住宿",
        "text": (
            "孝感住宿以孝南区（城区）酒店最集中；游双峰山、汤池可就近住景区周边。"
            "孝感在武汉城市圈内、城际通达，常作周末周边游。"
        ),
        "metadata": {"city": "孝感", "spot": "住宿", "source_site": "百度百科",
                      "source_url": _src("孝感"), "origin": "web",
                      "tags": ["孝感", "住宿", "孝南", "武汉城市圈"]},
    },
    {
        "type": "transport",
        "title": "孝感交通",
        "text": (
            "孝感东站（汉孝城际/高铁）至武汉约20-30分钟；距武汉天河机场约1小时，"
            "武汉城市圈内自驾与城际均便捷。"
        ),
        "metadata": {"city": "孝感", "spot": "交通", "source_site": "百度百科",
                      "source_url": _src("孝感"), "origin": "web",
                      "tags": ["孝感", "交通", "孝感东站", "城际", "武汉"]},
    },
    {
        "type": "season",
        "title": "孝感最佳旅游季节",
        "text": (
            "孝感春季（3-5月）踏青、秋季（9-11月）气候宜人；冬季可赴汤池温泉，"
            "夏季登双峰山避暑，四季皆有可游之处。"
        ),
        "metadata": {"city": "孝感", "spot": "最佳季节", "source_site": "百度百科",
                      "source_url": _src("孝感"), "origin": "web",
                      "tags": ["孝感", "最佳季节", "春秋", "温泉", "踏青"]},
    },
    # ===================== 随州 =====================
    {
        "type": "scenic",
        "title": "炎帝神农故里",
        "text": (
            "炎帝神农故里位于随县厉山镇，是华夏始祖炎帝神农传说的发源地，有神农大道、谒祖广场等，"
            "为寻根祭祖与中华农耕文化圣地，每年有谒祖大典。"
        ),
        "metadata": {"city": "随州", "spot": "炎帝神农故里", "source_site": "百度百科",
                      "source_url": _src("炎帝神农故里"), "origin": "web",
                      "tags": ["随州", "厉山", "炎帝", "神农", "寻根"]},
    },
    {
        "type": "scenic",
        "title": "大洪山",
        "text": (
            "大洪山横跨随州与京山，是佛教名山（洪山寺）与自然风景区，森林峡谷并存，"
            "秋季银杏与红叶知名，是礼佛与生态游兼具的目的地。"
        ),
        "metadata": {"city": "随州", "spot": "大洪山", "source_site": "百度百科",
                      "source_url": _src("大洪山"), "origin": "web",
                      "tags": ["随州", "大洪山", "佛教", "洪山寺", "红叶"]},
    },
    {
        "type": "scenic",
        "title": "擂鼓墩与曾侯乙墓遗址",
        "text": (
            "随州擂鼓墩是曾侯乙编钟（1978年出土，现藏湖北省博物馆）的出土地，"
            "为先秦礼乐文化重要遗址，是理解曾随之谜与楚文化的关键现场。"
        ),
        "metadata": {"city": "随州", "spot": "擂鼓墩", "source_site": "百度百科",
                      "source_url": _src("曾侯乙编钟"), "origin": "web",
                      "tags": ["随州", "擂鼓墩", "曾侯乙", "编钟", "楚文化"]},
    },
    {
        "type": "scenic",
        "title": "随州千年银杏谷",
        "text": (
            "千年银杏谷位于随州洛阳镇，拥有成片古银杏群落，每年11月中下旬至12月初金黄遍野，"
            "是湖北秋季标志性赏叶地（最佳窗口见“随州银杏季”）。"
        ),
        "metadata": {"city": "随州", "spot": "银杏谷", "source_site": "百度百科",
                      "source_url": _src("随州银杏"), "origin": "web",
                      "tags": ["随州", "洛阳镇", "银杏", "赏叶", "秋季"]},
    },
    {
        "type": "food",
        "title": "随州拐子饭与厉山腐乳",
        "text": (
            "拐子饭是随州代表性早餐（猪拐骨焖饭、咸香入味）；厉山腐乳为地方风味小菜，"
            "是随州家常味道的代表。"
        ),
        "metadata": {"city": "随州", "spot": "拐子饭", "source_site": "百度百科",
                      "source_url": _src("随州"), "origin": "web",
                      "tags": ["随州", "拐子饭", "厉山腐乳", "早餐", "小吃"]},
    },
    {
        "type": "stay",
        "title": "随州住宿",
        "text": (
            "随州住宿以曾都区（城区）酒店最便利；游大洪山、银杏谷可就近住景区周边乡镇。"
            "武汉至随州高铁约1小时，适合周末周边游。"
        ),
        "metadata": {"city": "随州", "spot": "住宿", "source_site": "百度百科",
                      "source_url": _src("随州"), "origin": "web",
                      "tags": ["随州", "住宿", "曾都", "高铁"]},
    },
    {
        "type": "transport",
        "title": "随州交通",
        "text": (
            "随州南站（高铁）通达武汉（约1小时）及十堰方向；汉十高速过境，市内至各景区需转乘汽车，"
            "建议自驾或包车串联大洪山、银杏谷等分散景点。"
        ),
        "metadata": {"city": "随州", "spot": "交通", "source_site": "百度百科",
                      "source_url": _src("随州"), "origin": "web",
                      "tags": ["随州", "交通", "随州南站", "汉十高铁", "自驾"]},
    },
    # ===================== 荆门 =====================
    {
        "type": "scenic",
        "title": "明显陵",
        "text": (
            "明显陵位于荆门钟祥市，是明嘉靖帝生父母恭睿献皇帝与章圣皇太后的合葬陵，"
            "世界文化遗产、明代帝陵规制代表作（门票信息见“明显陵门票与开放时间”）。"
        ),
        "metadata": {"city": "荆门", "spot": "明显陵", "source_site": "百度百科",
                      "source_url": _src("明显陵"), "origin": "web",
                      "tags": ["荆门", "钟祥", "明显陵", "世界遗产", "明陵"]},
    },
    {
        "type": "scenic",
        "title": "漳河风景区",
        "text": (
            "漳河风景区位于荆门西郊漳河水库一带，水面辽阔，可乘船游湖、体验水上运动，"
            "周边漳河新区为生态新城，是荆门近郊亲水去处。"
        ),
        "metadata": {"city": "荆门", "spot": "漳河", "source_site": "百度百科",
                      "source_url": _src("漳河"), "origin": "web",
                      "tags": ["荆门", "漳河", "水库", "游船", "亲水"]},
    },
    {
        "type": "scenic",
        "title": "绿林山",
        "text": (
            "绿林山位于荆门京山市，是东汉绿林起义发源地，有鸳鸯溪漂流、空山洞等，"
            "夏季避暑与亲子漂流热门，山岳与溶洞景观并存。"
        ),
        "metadata": {"city": "荆门", "spot": "绿林山", "source_site": "百度百科",
                      "source_url": _src("绿林山"), "origin": "web",
                      "tags": ["荆门", "京山", "绿林山", "漂流", "避暑"]},
    },
    {
        "type": "scenic",
        "title": "黄仙洞",
        "text": (
            "黄仙洞位于荆门钟祥大洪山余脉，为大型喀斯特溶洞，洞内石笋、钟乳、边石池壮观，"
            "与绿林山同属京山—钟祥旅游带，是探洞观光去处。"
        ),
        "metadata": {"city": "荆门", "spot": "黄仙洞", "source_site": "百度百科",
                      "source_url": _src("黄仙洞"), "origin": "web",
                      "tags": ["荆门", "钟祥", "黄仙洞", "溶洞", "喀斯特"]},
    },
    {
        "type": "food",
        "title": "荆门雪枣与钟祥蟠龙菜",
        "text": (
            "荆门雪枣为传统酥甜点心；钟祥蟠龙菜（传为嘉靖贡品，“吃肉不见肉”）是地方名菜，"
            "是荆门代表性风味。"
        ),
        "metadata": {"city": "荆门", "spot": "雪枣蟠龙菜", "source_site": "百度百科",
                      "source_url": _src("蟠龙菜"), "origin": "web",
                      "tags": ["荆门", "雪枣", "蟠龙菜", "钟祥", "贡菜"]},
    },
    {
        "type": "food",
        "title": "京山桥米",
        "text": (
            "京山桥米为历史贡米，米粒细长、口感软糯清香，是荆门代表性农产品与地理标志米，"
            "选购认准产地标识。"
        ),
        "metadata": {"city": "荆门", "spot": "桥米", "source_site": "百度百科",
                      "source_url": _src("京山桥米"), "origin": "web",
                      "tags": ["荆门", "京山", "桥米", "贡米", "地理标志"]},
    },
    {
        "type": "stay",
        "title": "荆门住宿",
        "text": (
            "荆门住宿以东宝区、掇刀区（城区）酒店最集中；游明显陵可住钟祥、绿林山住京山。"
            "武汉至荆门高铁约2小时，适合周末或两三日的周边游。"
        ),
        "metadata": {"city": "荆门", "spot": "住宿", "source_site": "百度百科",
                      "source_url": _src("荆门"), "origin": "web",
                      "tags": ["荆门", "住宿", "东宝", "钟祥", "京山"]},
    },
    {
        "type": "transport",
        "title": "荆门交通",
        "text": (
            "荆门西站（高铁）联通武汉（约2小时）、宜昌等；荆门站（普速）与漳河机场（支线）补充；"
            "市内公交衔接城区景点，远郊景区建议自驾或转乘。"
        ),
        "metadata": {"city": "荆门", "spot": "交通", "source_site": "百度百科",
                      "source_url": _src("荆门"), "origin": "web",
                      "tags": ["荆门", "交通", "荆门西站", "高铁", "漳河机场"]},
    },
    {
        "type": "season",
        "title": "荆门最佳旅游季节",
        "text": (
            "荆门春秋最宜（3-5月、9-11月），大洪山、绿林山植被丰茂、气候舒适；"
            "夏季可漂流避暑、冬季偏冷，春秋体感最佳。"
        ),
        "metadata": {"city": "荆门", "spot": "最佳季节", "source_site": "百度百科",
                      "source_url": _src("荆门"), "origin": "web",
                      "tags": ["荆门", "最佳季节", "春秋", "绿林山", "大洪山"]},
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
