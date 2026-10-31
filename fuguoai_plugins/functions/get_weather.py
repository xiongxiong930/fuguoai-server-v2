import os
import re
import time
import asyncio
import httpx
from bs4 import BeautifulSoup
from fuguoai_config.logger import setup_logging
from fuguoai_plugins.register import register_function, ToolType, ActionResponse, Action
from fuguoai_core.utils.util import get_ip_info
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fuguoai_core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

GET_WEATHER_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": (
            "获取某个地点的天气，用户应提供一个位置，比如用户说杭州天气，参数为：杭州。"
            "如果用户说的是省份，默认用省会城市。如果用户说的不是省份或城市而是一个地名，默认用该地所在省份的省会城市。"
            "重要：本地未来天气已在上下文中提供，用户未指明其他城市时绝对不要调用此工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "地点名，例如杭州。可选参数，如果不提供则不传",
                },
                "lang": {
                    "type": "string",
                    "description": "返回用户使用的语言code，例如zh_CN/zh_HK/en_US/ja_JP等，默认zh_CN",
                },
            },
            "required": ["lang"],
        },
    },
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/92.0.4515.107 Safari/537.36"
    )
}

# 和风天气代码 https://dev.qweather.com/docs/resource/icons/#weather-icons
WEATHER_CODE_MAP = {
    "100": "晴",
    "101": "多云",
    "102": "少云",
    "103": "晴间多云",
    "104": "阴",
    "150": "晴",
    "151": "多云",
    "152": "少云",
    "153": "晴间多云",
    "300": "阵雨",
    "301": "强阵雨",
    "302": "雷阵雨",
    "303": "强雷阵雨",
    "304": "雷阵雨伴有冰雹",
    "305": "小雨",
    "306": "中雨",
    "307": "大雨",
    "308": "极端降雨",
    "309": "毛毛雨/细雨",
    "310": "暴雨",
    "311": "大暴雨",
    "312": "特大暴雨",
    "313": "冻雨",
    "314": "小到中雨",
    "315": "中到大雨",
    "316": "大到暴雨",
    "317": "暴雨到大暴雨",
    "318": "大暴雨到特大暴雨",
    "350": "阵雨",
    "351": "强阵雨",
    "399": "雨",
    "400": "小雪",
    "401": "中雪",
    "402": "大雪",
    "403": "暴雪",
    "404": "雨夹雪",
    "405": "雨雪天气",
    "406": "阵雨夹雪",
    "407": "阵雪",
    "408": "小到中雪",
    "409": "中到大雪",
    "410": "大到暴雪",
    "456": "阵雨夹雪",
    "457": "阵雪",
    "499": "雪",
    "500": "薄雾",
    "501": "雾",
    "502": "霾",
    "503": "扬沙",
    "504": "浮尘",
    "507": "沙尘暴",
    "508": "强沙尘暴",
    "509": "浓雾",
    "510": "强浓雾",
    "511": "中度霾",
    "512": "重度霾",
    "513": "严重霾",
    "514": "大雾",
    "515": "特强浓雾",
    "900": "热",
    "901": "冷",
    "999": "未知",
}

# wttr.in 天气代码 → 中文
# 注意：wttr.in 的 lang=zh 并不翻译描述文字（current_condition/lang_zh 实测仍为英文），
# 因此中文必须靠本表自行映射（代码取自 wttr.in 使用的 World Weather Online 体系）。
WTTR_CODE_MAP = {
    "113": "晴",
    "116": "多云",
    "119": "阴",
    "122": "阴",
    "143": "薄雾",
    "176": "零星小雨",
    "179": "零星小雪",
    "182": "雨夹雪",
    "185": "冻毛毛雨",
    "200": "可能有雷阵雨",
    "227": "风吹雪",
    "230": "暴风雪",
    "248": "雾",
    "260": "冻雾",
    "263": "零星毛毛雨",
    "266": "毛毛雨",
    "281": "零星小雨",
    "284": "间歇性中雨",
    "293": "零星小雨",
    "296": "小雨",
    "299": "中雨",
    "302": "间歇性中雨",
    "305": "大雨",
    "308": "间歇性大雨",
    "311": "冻雨",
    "314": "冻雨",
    "317": "雨夹雪",
    "320": "雨夹雪",
    "323": "零星小雪",
    "326": "小雪",
    "329": "小到中雪",
    "332": "中雪",
    "335": "中到大雪",
    "338": "雪",
    "350": "冰粒",
    "353": "阵雨",
    "356": "阵雨",
    "359": "暴雨",
    "362": "阵雨夹雪",
    "365": "阵雨夹雪",
    "368": "阵雪",
    "371": "阵雪",
    "374": "冰粒",
    "377": "冰粒",
    "386": "雷阵雨",
    "389": "雷阵雨",
    "392": "雷阵雪",
    "395": "雷阵雪",
    "134": "沙尘暴",
    "149": "霾",
}

# ============================================================================
# 归一化天气图标枚举（provider 无关）
# ----------------------------------------------------------------------------
# 固件（屏幕/灯效）只能识别这一组固定枚举。后端在 wttr(WWO) 与 qweather 两套
# 原始代码体系之间切换数据源时，固件无需任何改动——它只认下面这组 enum。
# 同一"晴"在两套体系中码不同（wttr=113 / 和风=100），但都映射到 "sunny"。
NORMALIZED_WEATHER_ICONS = [
    "sunny",    # 晴
    "cloudy",   # 多云/少云/晴间多云
    "overcast", # 阴
    "fog",      # 雾/薄雾/冻雾
    "haze",     # 霾/沙尘/扬沙/浮尘
    "drizzle",  # 毛毛雨/零星小雨
    "rain",     # 雨/阵雨/中雨/大雨/暴雨
    "storm",    # 雷阵雨/雷阵雪/强雷阵雨
    "snow",     # 雪/小雪/中雪/大雪/暴雪/阵雪
    "sleet",    # 雨夹雪/冻雨/冰粒混合
    "hail",     # 冰粒/冰雹
    "unknown",  # 未知/其他
]

# wttr.in 使用 World Weather Online(WWO) 代码体系
WTTR_ICON_MAP = {
    "113": "sunny",
    "116": "cloudy", "102": "cloudy", "103": "cloudy",
    "119": "overcast", "122": "overcast",
    "143": "fog", "248": "fog", "260": "fog",
    "134": "haze", "149": "haze",
    "176": "drizzle", "185": "drizzle", "263": "drizzle",
    "266": "drizzle", "281": "drizzle", "293": "drizzle",
    "296": "rain", "299": "rain", "302": "rain", "305": "rain",
    "308": "rain", "353": "rain", "356": "rain", "359": "rain",
    "284": "rain",
    "200": "storm", "386": "storm", "389": "storm",
    "392": "storm", "395": "storm",
    "179": "snow", "227": "snow", "230": "snow",
    "323": "snow", "326": "snow", "329": "snow", "332": "snow",
    "335": "snow", "338": "snow", "368": "snow", "371": "snow",
    "182": "sleet", "317": "sleet", "320": "sleet",
    "350": "hail", "374": "hail", "377": "hail",
    "311": "sleet", "314": "sleet",
    "362": "sleet", "365": "sleet",
}

# 和风天气代码体系（仅 provider=qweather 时使用）
QWEATHER_ICON_MAP = {
    "100": "sunny", "150": "sunny",
    "101": "cloudy", "102": "cloudy", "103": "cloudy",
    "151": "cloudy", "152": "cloudy", "153": "cloudy",
    "104": "overcast",
    "500": "fog", "501": "fog", "509": "fog", "510": "fog", "514": "fog", "515": "fog",
    "502": "haze", "503": "haze", "504": "haze", "507": "haze", "508": "haze",
    "511": "haze", "512": "haze", "513": "haze",
    "300": "rain", "301": "rain", "305": "rain", "306": "rain", "307": "rain",
    "308": "rain", "310": "rain", "311": "rain", "312": "rain", "314": "rain",
    "315": "rain", "316": "rain", "317": "rain", "318": "rain", "350": "rain",
    "351": "rain", "399": "rain",
    "309": "drizzle",
    "313": "sleet",
    "302": "storm", "303": "storm", "304": "storm",
    "400": "snow", "401": "snow", "402": "snow", "403": "snow", "407": "snow",
    "408": "snow", "409": "snow", "410": "snow", "499": "snow",
    "404": "sleet", "405": "sleet", "406": "sleet", "456": "sleet", "457": "sleet",
    "900": "unknown", "901": "unknown", "999": "unknown",
}

# 中文天气描述 → 归一化枚举（raw_code 缺失时的兜底，比如和风未解析出当前码）
DESC_TO_ICON = {
    "晴": "sunny", "少云": "cloudy", "多云": "cloudy", "晴间多云": "cloudy",
    "阴": "overcast",
    "薄雾": "fog", "雾": "fog", "冻雾": "fog",
    "霾": "haze", "沙尘暴": "haze", "扬沙": "haze", "浮尘": "haze", "强沙尘暴": "haze",
    "毛毛雨": "drizzle", "零星小雨": "drizzle", "零星毛毛雨": "drizzle", "冻毛毛雨": "drizzle",
    "小雨": "rain", "中雨": "rain", "大雨": "rain", "暴雨": "rain",
    "间歇性中雨": "rain", "间歇性大雨": "rain", "阵雨": "rain", "强阵雨": "rain",
    "可能有雷阵雨": "storm", "雷阵雨": "storm", "强雷阵雨": "storm", "雷阵雪": "storm",
    "零星小雪": "snow", "小雪": "snow", "小到中雪": "snow", "中雪": "snow",
    "中到大雪": "snow", "大雪": "snow", "雪": "snow", "暴风雪": "snow",
    "风吹雪": "snow", "阵雪": "snow",
    "雨夹雪": "sleet", "冻雨": "sleet", "阵雨夹雪": "sleet",
    "冰粒": "hail",
}


def _fmt_temp(t):
    if t is None or t == "":
        return "未知"
    return str(t)


def _parse_num(s):
    """从 '30℃' / '65%' / '未知' 中抽取整数；抽不到返回 None。"""
    if s is None:
        return None
    m = re.search(r"-?\d+", str(s))
    return int(m.group()) if m else None


def parse_weather_info(soup):
    """解析和风天气网页（159 实测 100% 有效）。返回 (city, abstract, basic_dict, temps_list)。"""
    city_name = soup.select_one("h1.c-submenu__location").get_text(strip=True)

    current_abstract = soup.select_one(".c-city-weather-current .current-abstract")
    current_abstract = (
        current_abstract.get_text(strip=True) if current_abstract else "未知"
    )

    current_basic = {}
    for item in soup.select(
        ".c-city-weather-current .current-basic .current-basic___item"
    ):
        parts = item.get_text(strip=True, separator=" ").split(" ")
        if len(parts) == 2:
            key, value = parts[1], parts[0]
            current_basic[key] = value

    temps_list = []
    for row in soup.select(".city-forecast-tabs__row")[:7]:  # 取前7天的数据
        date = row.select_one(".date-bg .date").get_text(strip=True)
        weather_code = (
            row.select_one(".date-bg .icon")["src"].split("/")[-1].split(".")[0]
        )
        weather = WEATHER_CODE_MAP.get(weather_code, "未知")
        temps = [span.get_text(strip=True) for span in row.select(".tmp-cont .temp")]
        high_temp, low_temp = (temps[0], temps[-1]) if len(temps) >= 2 else (None, None)
        temps_list.append((date, weather, low_temp, high_temp))

    # 第 5 元 raw_code=None：和风当前天气码未在此处解析（由 DESC_TO_ICON 文本兜底）
    return city_name, current_abstract, current_basic, temps_list, None


def fetch_qweather(location, api_key, api_host, lang="zh"):
    """和风天气（需 key）：geo 反查 + 网页爬取。返回 (city, abstract, basic, temps) 或 None。"""
    try:
        with httpx.Client(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
            resp = client.get(
                f"https://{api_host}/geo/v2/city/lookup",
                params={"key": api_key, "location": location, "lang": "zh"},
                headers=HEADERS,
            )
            data = resp.json()
        if data.get("error") is not None or not data.get("location"):
            logger.bind(tag=TAG).debug(
                f"和风天气不可用（key 失效或网络错误）: {data.get('error', {}).get('detail')}"
            )
            return None
        fx_link = data["location"][0].get("fxLink")
        if not fx_link:
            return None
        with httpx.Client(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
            resp2 = client.get(fx_link, headers=HEADERS)
        if resp2.status_code != 200:
            return None
        return parse_weather_info(BeautifulSoup(resp2.text, "html.parser"))
    except Exception as e:
        logger.bind(tag=TAG).warning(f"和风天气获取失败: {e}")
        return None


def fetch_wttr(location, lang="zh"):
    """免 key 天气源 wttr.in。返回 (city, abstract, basic, temps) 或 None。

    说明：
      - wttr.in 支持中文城市名直接查询（广州/北京均可，数据与英文名一致）。
      - 默认仅返回未来 3 天（非 7 天）；切回和风后有 7 天。
      - 日高低温由 8 个 3 小时块的 tempC 聚合；午间代表取 time==1200 的天气代码。
      - wttr.in 的 nearest_area 中文解析不可靠（广州→Shekpai），城市名直接用入参。
    """
    try:
        with httpx.Client(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
            resp = client.get(
                "https://wttr.in/" + location,
                params={"format": "j1", "lang": "zh"},
                headers=HEADERS,
            )
        if resp.status_code != 200:
            logger.bind(tag=TAG).warning(f"wttr.in 返回 {resp.status_code}")
            return None
        d = resp.json()
        cur = d.get("current_condition", [{}])[0]
        code = str(cur.get("weatherCode", ""))
        current_abstract = WTTR_CODE_MAP.get(code, "未知")
        current_basic = {
            "气温": f"{cur.get('temp_C', '?')}℃",
            "体感": f"{cur.get('FeelsLikeC', '?')}℃",
            "湿度": f"{cur.get('humidity', '?')}%",
            "风速": f"{cur.get('windspeedKmph', '?')}km/h",
        }
        temps_list = []
        for day in d.get("weather", [])[:7]:
            date = day.get("date", "")
            hours = day.get("hourly", [])
            if not hours:
                continue
            tvals = [int(h.get("tempC", 0)) for h in hours if str(h.get("tempC", "")).isdigit()]
            low = min(tvals) if tvals else None
            high = max(tvals) if tvals else None
            mid = next((h for h in hours if h.get("time") == "1200"), hours[len(hours) // 2])
            day_code = str(mid.get("weatherCode", ""))
            weather = WTTR_CODE_MAP.get(day_code, "未知")
            temps_list.append((date, weather, low, high))
        return location, current_abstract, current_basic, temps_list, code
    except Exception as e:
        logger.bind(tag=TAG).warning(f"wttr.in 天气获取失败: {e}")
        return None


def generate_weather_report(location, lang, weather_config):
    """统一入口：按 provider 取数并拼中文报告；失败返回空串。

    provider:
      - wttr     : 免 key（默认），无需任何密钥，立即可用
      - qweather : 和风天气，需要 api_key（环境变量 QWEATHER_API_KEY 或 config 内 api_key）
    切换数据源只需改 config 一行，无需改代码。
    """
    provider = (weather_config.get("provider") or "wttr").lower()
    if provider == "qweather":
        api_key = os.getenv("QWEATHER_API_KEY") or weather_config.get("api_key", "")
        if not api_key:
            logger.bind(tag=TAG).warning(
                "provider=qweather 但未配置 API Key（请设环境变量 QWEATHER_API_KEY "
                "或 config 的 plugins.get_weather.api_key），无法获取天气"
            )
            return ""
        api_host = weather_config.get("api_host", "mj7p3y7naa.re.qweatherapi.com")
        result = fetch_qweather(location, api_key, api_host, lang)
    else:
        result = fetch_wttr(location, lang)
    if not result:
        return ""
    city_name, current_abstract, current_basic, temps_list, _raw_code = result
    return _build_report(city_name, current_abstract, current_basic, temps_list)


def _build_report(city_name, current_abstract, current_basic, temps_list):
    report = f"您查询的位置是：{city_name}\n\n当前天气: {current_abstract}\n"
    if current_basic:
        report += "详细参数：\n"
        for k, v in current_basic.items():
            if v not in (None, "", "0", "0℃", "0%", "0km/h", "?"):
                report += f"  · {k}: {v}\n"
    ndays = len(temps_list)
    if ndays >= 7:
        report += "\n未来7天预报：\n"
    elif ndays > 0:
        report += f"\n未来{ndays}天预报：\n"
    for date, weather, low, high in temps_list:
        report += f"{date}: {weather}，气温 {_fmt_temp(low)}~{_fmt_temp(high)}℃\n"
    report += "\n（如需某一天的具体天气，请告诉我日期）"
    return report


async def _resolve_location_str(location, weather_config, client_ip, logger):
    """解析查询城市：优先用入参；其次按客户端 IP 反查；最后兜底默认城市。

    与 _resolve_location(conn, ...) 同语义，但入参直接传 client_ip 字符串，
    以便 HTTP 接口（无 conn 对象）也能复用。
    """
    if location:
        return location
    default_location = weather_config.get("default_location", "广州")
    if client_ip:
        try:
            from fuguoai_core.utils.cache.manager import cache_manager, CacheType
            cached_ip = cache_manager.get(CacheType.IP_INFO, client_ip)
            if cached_ip and cached_ip.get("city"):
                return cached_ip["city"]
            ip_info = await asyncio.to_thread(get_ip_info, client_ip, logger)
            if ip_info:
                cache_manager.set(CacheType.IP_INFO, client_ip, ip_info)
                if ip_info.get("city"):
                    return ip_info["city"]
        except Exception:
            pass
    return default_location


async def get_weather_struct(location, weather_config, client_ip=None, lang="zh"):
    """结构化天气取数（供 HTTP 接口/固件使用）。

    返回精简 dict：{city, icon, desc, temp, high, low, humidity, provider, ts}
      - icon    : 归一化枚举（见 NORMALIZED_WEATHER_ICONS），固件只认这组，后端换数据源无感
      - desc    : 中文天气描述
      - temp    : 当前温度(℃)，int 或 None
      - high/low: 今日最高/最低温(℃)，int 或 None
      - humidity: 当前湿度(%)，int 或 None
      - provider: "wttr" / "qweather"
      - ts      : 取数时刻 Unix 时间戳
    返回 None 表示取数失败（网络/配置）。不破坏语音链路。
    """
    location = await _resolve_location_str(location, weather_config, client_ip, logger)
    if not location:
        return None
    provider = (weather_config.get("provider") or "wttr").lower()
    if provider == "qweather":
        api_key = os.getenv("QWEATHER_API_KEY") or weather_config.get("api_key", "")
        if not api_key:
            logger.bind(tag=TAG).warning(
                "provider=qweather 但未配置 API Key，无法获取结构化天气"
            )
            return None
        api_host = weather_config.get("api_host", "mj7p3y7naa.re.qweatherapi.com")
        result = await asyncio.to_thread(fetch_qweather, location, api_key, api_host, lang)
    else:
        result = await asyncio.to_thread(fetch_wttr, location, lang)
    if not result:
        return None
    city_name, current_abstract, current_basic, temps_list, raw_code = result

    temp = _parse_num(current_basic.get("气温"))
    humidity = _parse_num(current_basic.get("湿度"))
    high = low = None
    if temps_list:
        _, _, low, high = temps_list[0]
        # wttr 高低温为 int，和风为字符串，统一转 int 给固件一致报文
        high = _parse_num(high)
        low = _parse_num(low)

    if provider == "qweather":
        icon = QWEATHER_ICON_MAP.get(raw_code) or DESC_TO_ICON.get(current_abstract, "unknown")
    else:
        icon = WTTR_ICON_MAP.get(raw_code) or DESC_TO_ICON.get(current_abstract, "unknown")

    return {
        "city": city_name,
        "icon": icon,
        "desc": current_abstract,
        "temp": temp,
        "high": high,
        "low": low,
        "humidity": humidity,
        "provider": provider,
        "ts": int(time.time()),
    }


@register_function("get_weather", GET_WEATHER_FUNCTION_DESC, ToolType.SYSTEM_CTL)
async def get_weather(conn: "ConnectionHandler", location: str = None, lang: str = "zh_CN"):
    from fuguoai_core.utils.cache.manager import cache_manager, CacheType

    weather_config = conn.config.get("plugins", {}).get("get_weather", {})
    provider = (weather_config.get("provider") or "wttr").lower()
    location = await _resolve_location_str(
        location, weather_config, getattr(conn, "client_ip", None), logger
    )

    # 缓存 key 含 provider，切换数据源后自动失效
    cache_key = f"full_weather_{provider}_{location}_{lang}"
    cached = cache_manager.get(CacheType.WEATHER, cache_key)
    if cached is not None:
        return ActionResponse(Action.REQLLM, cached, None)

    # 同步网络请求放到线程池，避免阻塞事件循环
    report = await asyncio.to_thread(generate_weather_report, location, lang, weather_config)
    # 仅缓存成功结果，失败不缓存以免长期掩盖配置修复
    if report:
        cache_manager.set(CacheType.WEATHER, cache_key, report)
    return ActionResponse(Action.REQLLM, report, None)
