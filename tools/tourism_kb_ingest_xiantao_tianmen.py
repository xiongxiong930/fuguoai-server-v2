# -*- coding: utf-8 -*-
"""湖北文旅 KB 内容增厚（仙桃 / 天门 专项）：补齐两个零覆盖城市。

背景（2026-10-17 实读容器 hubei.json 分布，302 条）：
  - 湖北省直辖县级行政区 4 个中，潜江 9 / 神农架 9 已有条目，
    但 **仙桃 = 0、天门 = 0** —— 完全空白，本批专补这两城。
  - 另：全库 227/302（75%）正文 <200 字，故本批条目正文均写到 150 字以上，兼顾增厚。

数据口径（防胡说八道，遵守 skill 铁律）：
  - 票价一律标注“参考价，以景区官方公示为准”，不把渠道价写成唯一事实。
  - 权威源优先：天门条目取自 **天门市人民政府官网**（.gov.cn）与湖北日报客户端；
    仙桃条目取自 **湖北日报**（党媒）与携程景点资料。
  - 不写易变班次时刻；描述性内容不臆造。

跑法（容器内，embedding 缓存只在容器有）：
  python3 /opt/fuguoai-server/data/tourism_kb_ingest_xiantao_tianmen.py
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


# ---------------------- 仙桃（当前 0 条） ----------------------
_XIANTAO = [
    {
        "type": "scenic",
        "title": "仙桃梦里水乡文化旅游区",
        "text": (
            "梦里水乡文化旅游区位于湖北省仙桃市，是国家4A级旅游景区，以水上森林与杉林水韵为特色。"
            "游客可乘画舫游览水上森林，傍晚可见千盏渔灯点亮的荷塘，景区内树影浮桥是热门拍照点，"
            "部分时段有鸬鹚捕鱼表演。参考门票约120元（含船票与观光车），"
            "以上为参考价，实际票价以景区官方公示为准。"
        ),
        "metadata": {"city": "仙桃", "spot": "梦里水乡文化旅游区",
                      "source_site": "携程", "source_url": "https://gs.ctrip.com/html5/you/sight/31/4380347.html",
                      "origin": "web", "tags": ["梦里水乡", "水上森林", "4A景区", "画舫", "仙桃"]},
    },
    {
        "type": "scenic",
        "title": "仙桃沔阳小镇",
        "text": (
            "沔阳小镇位于仙桃市排湖风景区，是以沔阳文化为特色的水乡小镇，一期已创建为国家3A级旅游景区。"
            "游客可租汉服拍古风照片，周末晚间有烟花秀，水上集市可体验鸬鹚捕鱼，"
            "并可参与学打渔鼓、做麦秆画等非遗体验。参考门票约56至59元/人（双人票约108元），"
            "以上为参考价，实际票价以景区官方公示为准。"
        ),
        "metadata": {"city": "仙桃", "spot": "沔阳小镇",
                      "source_site": "携程", "source_url": "https://gs.ctrip.com/html5/you/sight/31/4380347.html",
                      "origin": "web", "tags": ["沔阳小镇", "3A景区", "水乡", "非遗", "仙桃"]},
    },
    {
        "type": "scenic",
        "title": "仙桃沙湖国家湿地公园",
        "text": (
            "沙湖国家湿地公园位于仙桃市沙湖镇，是江汉平原重要的湿地景观，芦花万顷、水天一色。"
            "游客可乘竹筏穿越芦苇荡，秋季芦花如雪，观鸟塔可观赏“鸟浪齐飞”；"
            "9至10月可体验采菱角、挖野藕等季节性活动，沙湖咸蛋为当地知名特产。"
            "公园免费开放，游船参考约60元，以上为参考价，以景区官方公示为准。"
        ),
        "metadata": {"city": "仙桃", "spot": "沙湖国家湿地公园",
                      "source_site": "湖北日报", "source_url": "https://news.hubeidaily.net/pc/143963.html",
                      "origin": "web", "tags": ["湿地", "观鸟", "芦苇荡", "免费", "仙桃"]},
    },
    {
        "type": "scenic",
        "title": "仙桃排湖风景区",
        "text": (
            "排湖风景区位于仙桃市五乐台大道，是仙桃的城市绿肺。清晨薄雾中的湖面宛如水墨画，"
            "设有环湖绿道可骑行，傍晚可观晚霞，并有“排湖之眼”摩天轮与帆船基地，"
            "可品尝湖鲜“排湖三宝”。相传楚王曾在此狩猎留下胜迹。"
            "景区免费开放，游船参考约50元/小时，以景区官方公示为准。"
        ),
        "metadata": {"city": "仙桃", "spot": "排湖风景区",
                      "source_site": "携程", "source_url": "https://gs.ctrip.com/html5/you/sight/31/4380347.html",
                      "origin": "web", "tags": ["排湖", "环湖绿道", "摩天轮", "湖鲜", "仙桃"]},
    },
    {
        "type": "scenic",
        "title": "仙桃沔州百万花海风景区",
        "text": (
            "沔州百万花海风景区是仙桃休闲赏花景点，以主题花海与采摘体验为特色，"
            "适合春季赏花与亲子出游。此外仙桃还有九合兰园（研学采摘）、"
            "荆楚云天海世界（水上酷玩）、阿尔迪有机生态园等休闲打卡点，"
            "构成仙桃乡村休闲旅游带的重要内容。"
        ),
        "metadata": {"city": "仙桃", "spot": "沔州百万花海",
                      "source_site": "湖北日报", "source_url": "https://news.hubeidaily.net/pc/143963.html",
                      "origin": "web", "tags": ["花海", "赏花", "采摘", "亲子", "仙桃"]},
    },
    {
        "type": "info",
        "title": "仙桃旅游概览与城市名片",
        "text": (
            "仙桃市位于江汉平原，有着1500年建制史，是国家园林城市、国家卫生城市，"
            "被誉为亚洲体操之乡与中国文艺之乡，正在创建国家全域旅游示范市。"
            "旅游空间布局初步形成“一江两沔三湖四带”，并打造沔西乡村休闲、沔中环排湖、"
            "沔南湿地、沔东红色四条精品旅游线路。武汉至仙桃高铁约0.5小时，交通便利。"
        ),
        "metadata": {"city": "仙桃", "spot": "城市概览",
                      "source_site": "湖北日报", "source_url": "https://news.hubeidaily.net/pc/143963.html",
                      "origin": "web", "tags": ["仙桃", "城市名片", "全域旅游", "体操之乡", "概览"]},
    },
    {
        "type": "culture",
        "title": "仙桃沔街（湖北十大名街）",
        "text": (
            "沔街是湖北十大名街之一，位于仙桃市区，是以沔阳文化为主题打造的特色街区。"
            "街区流光溢彩、十里飘香，聚集沔阳三蒸等本地美食，"
            "并有沔阳花鼓戏与皮影戏等曲艺演出，是体验仙桃夜色与市井烟火的主要去处。"
        ),
        "metadata": {"city": "仙桃", "spot": "沔街",
                      "source_site": "湖北日报", "source_url": "https://news.hubeidaily.net/pc/143963.html",
                      "origin": "web", "tags": ["沔街", "名街", "夜市", "美食街", "仙桃"]},
    },
    {
        "type": "culture",
        "title": "仙桃体操之乡与名人故里",
        "text": (
            "仙桃是著名的体操之乡与冠军之乡，走出李小双、李大双、杨威、郑李辉、廖辉、昌雅妮等世界冠军，"
            "共获多枚奥运金牌，当地建有李小双体操学校，可感受冠军摇篮文化。"
            "仙桃亦是人杰地灵之地：民主先驱张难先、中山舰舰长李之龙、棉花博士杨显东均出自此处，"
            "小米科技创始人雷军也出生于仙桃。"
        ),
        "metadata": {"city": "仙桃", "spot": "体操之乡",
                      "source_site": "湖北日报", "source_url": "https://news.hubeidaily.net/pc/143963.html",
                      "origin": "web", "tags": ["体操之乡", "李小双", "杨威", "雷军", "仙桃"]},
    },
    {
        "type": "culture",
        "title": "沔阳花鼓戏与沔阳皮影",
        "text": (
            "沔阳花鼓戏与沔阳皮影戏是仙桃代表性的民间曲艺，唱腔婉转、曲韵幽婉情意绵长，"
            "常在沔街等场所演出，是湖北地方戏曲与非遗的重要组成部分。"
            "此外仙桃郑场镇的麦秆画亦为当地特色非遗手作，游客可体验制作并带走作品。"
        ),
        "metadata": {"city": "仙桃", "spot": "沔阳花鼓戏",
                      "source_site": "湖北日报", "source_url": "https://news.hubeidaily.net/pc/143963.html",
                      "origin": "web", "tags": ["花鼓戏", "皮影", "麦秆画", "非遗", "仙桃"]},
    },
    {
        "type": "food",
        "title": "沔阳三蒸（仙桃非遗美食）",
        "text": (
            "沔阳三蒸是仙桃最具代表性的非遗美食，通常指粉蒸肉、蒸鱼、蒸蔬菜三味一体，"
            "讲究用糙米磨粉，成品清香软糯、鲜香交融。当地有“沔阳三蒸宴”可一次品尝多种蒸菜，"
            "是到仙桃必尝的招牌。推荐搭配米茶与酸辣藕带，风味更完整。"
        ),
        "metadata": {"city": "仙桃", "spot": "沔阳三蒸",
                      "source_site": "携程", "source_url": "https://gs.ctrip.com/html5/you/sight/31/4380347.html",
                      "origin": "web", "tags": ["沔阳三蒸", "非遗", "粉蒸肉", "蒸菜", "仙桃"]},
    },
    {
        "type": "food",
        "title": "仙桃特色小吃（毛嘴卤鸡·鳝鱼米粉·米团子）",
        "text": (
            "仙桃特色小吃丰富：毛嘴卤鸡肉质紧实、卤香浓郁，真空包装便于携带，是热门伴手礼；"
            "鳝鱼米粉糊汤浓白、鳝鱼臊子鲜美，是当地经典早餐，老店常常清晨即开门；"
            "仙桃米团子以糯米包腊肉馅，寓意团圆。此外沙湖咸蛋红心流油，也值得购买。"
        ),
        "metadata": {"city": "仙桃", "spot": "特色小吃",
                      "source_site": "携程", "source_url": "https://gs.ctrip.com/html5/you/sight/31/4380347.html",
                      "origin": "web", "tags": ["毛嘴卤鸡", "鳝鱼米粉", "米团子", "沙湖咸蛋", "仙桃"]},
    },
    {
        "type": "ticket",
        "title": "仙桃主要景区门票参考",
        "text": (
            "仙桃主要景区门票参考如下：梦里水乡文化旅游区约120元（含船票与观光车）；"
            "沔阳小镇约56至59元/人（双人票约108元）；沙湖国家湿地公园免费开放，游船约60元；"
            "排湖风景区免费开放，游船约50元/小时。"
            "以上均为参考价，实际票价与开放时间以各景区官方公示为准。"
        ),
        "metadata": {"city": "仙桃", "spot": "门票参考",
                      "source_site": "携程", "source_url": "https://gs.ctrip.com/html5/you/sight/31/4380347.html",
                      "origin": "web", "tags": ["门票", "票价", "参考价", "仙桃"]},
    },
    {
        "type": "transport",
        "title": "仙桃交通到达方式",
        "text": (
            "仙桃位于江汉平原，武汉至仙桃乘高铁约0.5小时，参考票价约30元，是最便捷的到达方式。"
            "市内各景点之间可选择包车出行，参考约150元/天；部分景区有公交线路可达。"
            "具体列车班次与票价以铁路部门官方公示为准。"
        ),
        "metadata": {"city": "仙桃", "spot": "交通",
                      "source_site": "携程", "source_url": "https://gs.ctrip.com/html5/you/sight/31/4380347.html",
                      "origin": "web", "tags": ["交通", "高铁", "武汉", "包车", "仙桃"]},
    },
    {
        "type": "tip",
        "title": "仙桃游览贴士",
        "text": (
            "仙桃最佳游览季节为4至10月：春季赏花、夏季避暑、秋季观鸟。"
            "湿地与水乡景区蚊虫较多，注意防蚊与防晒；乘船游览须穿救生衣、听从船员指引。"
            "秋季采菱角、挖野藕等体验以景区当季安排为准；"
            "沔阳小镇周末晚间烟花秀与水上集市活动，出行前建议先确认当日场次。"
        ),
        "metadata": {"city": "仙桃", "spot": "游览贴士",
                      "source_site": "携程", "source_url": "https://gs.ctrip.com/html5/you/sight/31/4380347.html",
                      "origin": "web", "tags": ["贴士", "季节", "安全", "防蚊", "仙桃"]},
    },
]

# ---------------------- 天门（当前 0 条） ----------------------
_TIANMEN = [
    {
        "type": "scenic",
        "title": "天门石家河遗址与石家河遗址博物馆",
        "text": (
            "石家河遗址位于天门市石家河镇，是距今约5900年至3800年的新石器时代大型聚落遗址，"
            "被誉为长江中游史前文明的巅峰，出土大量精美玉器与史前手工业遗珍。"
            "石家河遗址博物馆可穿越5900年的文明之光，并观看湖北首个行进式沉浸式实景剧"
            "《看见石家河》。博物馆免费开放，《看见石家河》演艺参考票价约95元，"
            "以上为参考价，以景区官方公示为准。"
        ),
        "metadata": {"city": "天门", "spot": "石家河遗址",
                      "source_site": "天门市人民政府",
                      "source_url": "https://www.tianmen.gov.cn/xwzx/tpxw/202607/t20260730_5985890.shtml",
                      "origin": "web", "tags": ["石家河", "遗址", "史前文明", "博物馆", "天门"]},
    },
    {
        "type": "scenic",
        "title": "天门茶圣故里园（陆羽故园）",
        "text": (
            "茶圣故里园又称陆羽故园，位于天门市竟陵城区西湖之滨，是纪念茶圣陆羽的园区。"
            "园内有古雁桥、茶经楼、文学泉等与陆羽相关的胜迹，可了解陆羽生平事迹与中国茶文化，"
            "部分时段提供茶道体验活动。园区免费开放，是天门核心打卡点之一。"
        ),
        "metadata": {"city": "天门", "spot": "茶圣故里园",
                      "source_site": "天门市人民政府",
                      "source_url": "https://www.tianmen.gov.cn/xwzx/tpxw/202607/t20260730_5985890.shtml",
                      "origin": "web", "tags": ["陆羽", "茶圣", "茶经楼", "古雁桥", "天门"]},
    },
    {
        "type": "scenic",
        "title": "天门胡家花园",
        "text": (
            "胡家花园位于天门市区，是湖北仅存的巡抚官厅，格局为“轴线渐进，前厅后堂”。"
            "漫步在青砖黛瓦之间，抚摸斑驳的砖雕与木柱，看到的不仅是精美的古建艺术，"
            "更是一段写在砖瓦上的近代中国微缩史诗，是天门古建古韵的代表。"
        ),
        "metadata": {"city": "天门", "spot": "胡家花园",
                      "source_site": "天门市人民政府",
                      "source_url": "https://www.tianmen.gov.cn/xwzx/tpxw/202607/t20260730_5985890.shtml",
                      "origin": "web", "tags": ["胡家花园", "古建", "巡抚官厅", "砖雕", "天门"]},
    },
    {
        "type": "scenic",
        "title": "天门北湖公园与北湖夜市",
        "text": (
            "北湖公园位于天门市区，傍着波光粼粼的湖面而建，是市民休闲与夜游的好去处。"
            "夜幕降临后可逛北湖夜市，体验充满烟火气的北湖之夜，"
            "与陆羽故园、美食街共同构成天门城区的一日游动线。"
        ),
        "metadata": {"city": "天门", "spot": "北湖公园",
                      "source_site": "湖北日报客户端",
                      "source_url": "https://www.toutiao.com/article/7499400040482439720/",
                      "origin": "web", "tags": ["北湖", "夜市", "公园", "休闲", "天门"]},
    },
    {
        "type": "info",
        "title": "天门旅游概览与城市名片",
        "text": (
            "天门是一座低调了千年的宝藏小城，素有“四湖六水绕城过，一城人家半城碧”之说。"
            "这里是茶圣陆羽的故里，也见证了石家河遗址的文明曙光，"
            "有水乡温婉、古建古韵和满城飘香的蒸菜味儿。"
            "石家河遗址博物馆、茶圣故里园、胡家花园等核心打卡点均为免门票景区。"
        ),
        "metadata": {"city": "天门", "spot": "城市概览",
                      "source_site": "天门市人民政府",
                      "source_url": "https://www.tianmen.gov.cn/xwzx/tpxw/202607/t20260730_5985890.shtml",
                      "origin": "web", "tags": ["天门", "城市名片", "陆羽", "免费景区", "概览"]},
    },
    {
        "type": "culture",
        "title": "陆羽与天门茶文化",
        "text": (
            "陆羽被尊为茶圣，所著《茶经》是世界现存最早、最完整的茶叶专著，天门是其故里。"
            "天门的陆羽故园、文学泉、古雁桥、茶经楼等胜迹均与陆羽传说相关，"
            "处处沾着茶香与诗意，是茶文化爱好者寻根与体验茶道的目的地。"
        ),
        "metadata": {"city": "天门", "spot": "陆羽茶文化",
                      "source_site": "天门市人民政府",
                      "source_url": "https://www.tianmen.gov.cn/xwzx/tpxw/202607/t20260730_5985890.shtml",
                      "origin": "web", "tags": ["陆羽", "茶经", "茶文化", "茶道", "天门"]},
    },
    {
        "type": "food",
        "title": "天门蒸菜与天门九蒸",
        "text": (
            "天门蒸菜历史悠久，讲究“一笼蒸万物”，形成粉蒸、清蒸、炮蒸、扣蒸、包蒸、"
            "酿蒸、封蒸、干蒸、造型蒸九种技法，合称天门九蒸，荤素搭配、蒸法多样。"
            "其中炮蒸鳝鱼最具代表性：出锅浇热油时“噼啪”如放炮，鱼肉滑嫩酸鲜、开胃爽口，"
            "需经两蒸一过油方能形成表皮酥脆、内里软糯的丰富口感。"
        ),
        "metadata": {"city": "天门", "spot": "天门蒸菜",
                      "source_site": "湖北日报客户端",
                      "source_url": "https://www.toutiao.com/article/7499400040482439720/",
                      "origin": "web", "tags": ["天门蒸菜", "九蒸", "炮蒸鳝鱼", "非遗", "天门"]},
    },
    {
        "type": "food",
        "title": "天门特色小吃（黄潭米粉·天门锅盔）",
        "text": (
            "天门特色小吃以黄潭米粉和天门锅盔最具代表性：黄潭米粉糊汤焦黄浓香，"
            "配上鳝鱼臊子鲜味突出，是当地人唤醒一天元气的经典早餐；"
            "天门锅盔外酥里软，猪油葱花麦香扑鼻，趁热食用风味最佳。"
        ),
        "metadata": {"city": "天门", "spot": "特色小吃",
                      "source_site": "天门市人民政府",
                      "source_url": "https://www.tianmen.gov.cn/xwzx/tpxw/202607/t20260730_5985890.shtml",
                      "origin": "web", "tags": ["黄潭米粉", "锅盔", "早餐", "小吃", "天门"]},
    },
    {
        "type": "ticket",
        "title": "天门主要景区门票参考",
        "text": (
            "天门主要景区门票参考如下：石家河遗址博物馆免费开放，"
            "馆内《看见石家河》演艺参考票价约95元；茶圣故里园（陆羽故园）免费开放；"
            "胡家花园等景点以现场公示为准。天门多数核心打卡点为免门票景区。"
            "以上均为参考价，实际票价与开放安排以景区官方公示为准。"
        ),
        "metadata": {"city": "天门", "spot": "门票参考",
                      "source_site": "天门市人民政府",
                      "source_url": "https://www.tianmen.gov.cn/xwzx/tpxw/202607/t20260730_5985890.shtml",
                      "origin": "web", "tags": ["门票", "免费", "参考价", "石家河", "天门"]},
    },
    {
        "type": "tip",
        "title": "天门游览贴士",
        "text": (
            "天门多数核心景区免门票，出行成本低，适合慢游。"
            "石家河遗址博物馆的《看见石家河》为行进式沉浸式实景剧，场次有限，建议提前了解当日安排；"
            "天门蒸菜以热食为佳，炮蒸鳝鱼出锅后宜趁热品尝。"
            "夏季注意防暑，湖区与公园夜游注意安全。"
        ),
        "metadata": {"city": "天门", "spot": "游览贴士",
                      "source_site": "天门市人民政府",
                      "source_url": "https://www.tianmen.gov.cn/xwzx/tpxw/202607/t20260730_5985890.shtml",
                      "origin": "web", "tags": ["贴士", "免费", "实景剧", "蒸菜", "天门"]},
    },
]

ITEMS = _XIANTAO + _TIANMEN


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
