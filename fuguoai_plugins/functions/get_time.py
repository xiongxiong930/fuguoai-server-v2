"""农历 / 黄历查询插件（复果自有实现，clean-room 重写）。

对外注册函数名 "get_lunar"，返回给 LLM 的文本包含农历日期、干支、
生肖八字、节气、宜忌等黄历信息。结果按日缓存，避免重复计算。

实现说明：黄历字段清单以"字段表"形式声明（标签 + 取值函数），
与 cnlunar 库的取值接口一一对应；响应文本由字段表统一渲染。
"""

from datetime import datetime, timedelta, timezone

import cnlunar

from fuguoai_plugins.register import (
    Action,
    ActionResponse,
    ToolType,
    register_function,
)

# 农历按北京时区（UTC+8）计算
_BEIJING_TZ = timezone(timedelta(hours=8))
_DATE_FORMAT = "%Y-%m-%d"
_DEFAULT_QUERY = "默认查询干支年和农历日期"

get_lunar_function_desc = {
    "type": "function",
    "function": {
        "name": "get_lunar",
        "description": (
            "用于具体日期的阴历/农历和黄历信息。"
            "用户可以指定查询内容，如：阴历日期、天干地支、节气、生肖、星座、八字、宜忌等。"
            "如果没有指定查询内容，则默认查询干支年和农历日期。"
            "对于'今天农历是多少'、'今天农历日期'这样的基本查询，请直接使用context中的信息，不要调用此工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "要查询的日期，格式为YYYY-MM-DD，例如2024-01-01。如果不提供，则使用当前日期",
                },
                "query": {
                    "type": "string",
                    "description": "要查询的内容，例如阴历日期、天干地支、节日、节气、生肖、星座、八字、宜忌等",
                },
            },
            "required": [],
        },
    },
}


def _holidays_text(lunar) -> str:
    """法定节日 + 其它节日 + 其它农历节日，逗号拼接（空段过滤）。"""
    parts = (
        lunar.get_legalHolidays(),
        lunar.get_otherHolidays(),
        lunar.get_otherLunarHolidays(),
    )
    return ",".join(x for x in parts if x)


def _solar_terms_text(lunar) -> str:
    """全年节气表：`节气名(M月D日)`，逗号+空格拼接。"""
    items = []
    for term, md in lunar.thisYearSolarTermsDic.items():
        items.append(f"{term}({md[0]}月{md[1]}日)")
    return ", ".join(items)


def _eight_chars_text(lunar) -> str:
    """四柱八字：年柱 月柱 日柱 时柱，空格拼接。"""
    pillars = (lunar.year8Char, lunar.month8Char, lunar.day8Char, lunar.twohour8Char)
    return " ".join(pillars)


def _officer_text(lunar) -> str:
    """值日 / 值神（十二值神只取一次，避免重复调用）。"""
    officer = lunar.get_today12DayOfficer()
    return f"值日: {officer[0]}执位\n值神: {officer[1]}({officer[2]})"


# 黄历字段表：(渲染标签, 取值函数)。顺序即响应文本行序。
_LUNAR_FIELDS = (
    ("农历日期", lambda L: f"{L.lunarYearCn}年{L.lunarMonthCn[:-1]}{L.lunarDayCn}"),
    ("干支", lambda L: f"{L.year8Char}年 {L.month8Char}月 {L.day8Char}日"),
    ("生肖", lambda L: f"属{L.chineseYearZodiac}"),
    ("八字", _eight_chars_text),
    ("今日节日", _holidays_text),
    ("今日节气", lambda L: f"{L.todaySolarTerms}"),
    ("下一节气", lambda L: (
        f"{L.nextSolarTerm} {L.nextSolarTermYear}年"
        f"{L.nextSolarTermDate[0]}月{L.nextSolarTermDate[1]}日"
    )),
    ("今年节气表", _solar_terms_text),
    ("生肖冲煞", lambda L: f"{L.chineseZodiacClash}"),
    ("星座", lambda L: f"{L.starZodiac}"),
    ("纳音", lambda L: f"{L.get_nayin()}"),
    ("彭祖百忌", lambda L: f"{L.get_pengTaboo(delimit=', ')}"),
    ("值日/值神", _officer_text),
    ("廿八宿", lambda L: f"{L.get_the28Stars()}"),
    ("吉神方位", lambda L: f"{' '.join(L.get_luckyGodsDirection())}"),
    ("今日胎神", lambda L: f"{L.get_fetalGod()}"),
    ("宜", lambda L: f"{'、'.join(L.goodThing[:10])}"),
    ("忌", lambda L: f"{'、'.join(L.badThing[:10])}"),
)

_RESPONSE_PREAMBLE = (
    "根据以下信息回应用户的查询请求，并提供与{query}相关的信息：",
    "农历信息：",
)
_RESPONSE_POSTAMBLE = (
    "(默认返回干支年和农历日期；仅在要求查询宜忌信息时才返回本日宜忌)",
)


def _render_lunar_response(lunar, query: str) -> str:
    """按字段表把 cnlunar 计算结果渲染成给 LLM 的文本。"""
    lines = [seg.format(query=query) for seg in _RESPONSE_PREAMBLE]
    for label, fetch in _LUNAR_FIELDS:
        value = fetch(lunar)
        if label == "农历日期":
            # 首行无标签，直接是"X年X月X日"形态
            lines.append(value)
        elif label == "值日/值神":
            # 两行字段，已自带"值日:/值神:"前缀
            lines.append(value)
        else:
            lines.append(f"{label}: {value}")
    lines.extend(_RESPONSE_POSTAMBLE)
    return "\n".join(lines)


def _parse_date_or_none(date):
    """把 YYYY-MM-DD 解析为 naive datetime；格式非法返回 None。

    ⚠️ 只捕 ValueError：与重写前版本异常面逐字节一致
    （非字符串 date 的 TypeError 同样向上抛，不在此收敛）。
    """
    try:
        return datetime.strptime(date, _DATE_FORMAT)
    except ValueError:
        return None


@register_function("get_lunar", get_lunar_function_desc, ToolType.WAIT)
def get_lunar(date=None, query=None):
    """查询指定日期（默认今天，北京时区）的农历与黄历信息。"""
    from fuguoai_core.utils.cache.manager import CacheType, cache_manager

    if date:
        parsed = _parse_date_or_none(date)
        if parsed is None:
            return ActionResponse(
                Action.REQLLM,
                "日期格式错误，请使用YYYY-MM-DD格式，例如：2024-01-01",
                None,
            )
        now = parsed
    else:
        now = datetime.now(_BEIJING_TZ)

    if query is None:
        query = _DEFAULT_QUERY

    day = now.strftime(_DATE_FORMAT)
    cache_key = f"lunar_info_{day}"
    cached = cache_manager.get(CacheType.LUNAR, cache_key)
    if cached:
        return ActionResponse(Action.REQLLM, cached, None)

    lunar = cnlunar.Lunar(now, godType="8char")
    response_text = _render_lunar_response(lunar, query)
    cache_manager.set(CacheType.LUNAR, cache_key, response_text)

    return ActionResponse(Action.REQLLM, response_text, None)
