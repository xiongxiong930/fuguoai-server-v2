"""农历 / 黄历查询插件（复果自有实现）。

对外注册函数名 "get_lunar"，返回给 LLM 的文本包含农历日期、干支、
生肖八字、节气、宜忌等黄历信息。结果按日缓存，避免重复计算。
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


def _build_lunar_text(lunar, query: str) -> str:
    """把 cnlunar 的计算结果拼成给 LLM 的文本。"""
    officer = lunar.get_today12DayOfficer()
    holidays = ",".join(
        filter(
            None,
            (
                lunar.get_legalHolidays(),
                lunar.get_otherHolidays(),
                lunar.get_otherLunarHolidays(),
            ),
        )
    )
    solar_terms = ", ".join(
        f"{term}({month}月{day}日)"
        for term, (month, day) in lunar.thisYearSolarTermsDic.items()
    )
    eight_chars = " ".join(
        [lunar.year8Char, lunar.month8Char, lunar.day8Char, lunar.twohour8Char]
    )

    lines = [
        f"根据以下信息回应用户的查询请求，并提供与{query}相关的信息：",
        "农历信息：",
        f"{lunar.lunarYearCn}年{lunar.lunarMonthCn[:-1]}{lunar.lunarDayCn}",
        f"干支: {lunar.year8Char}年 {lunar.month8Char}月 {lunar.day8Char}日",
        f"生肖: 属{lunar.chineseYearZodiac}",
        f"八字: {eight_chars}",
        f"今日节日: {holidays}",
        f"今日节气: {lunar.todaySolarTerms}",
        f"下一节气: {lunar.nextSolarTerm} {lunar.nextSolarTermYear}年"
        f"{lunar.nextSolarTermDate[0]}月{lunar.nextSolarTermDate[1]}日",
        f"今年节气表: {solar_terms}",
        f"生肖冲煞: {lunar.chineseZodiacClash}",
        f"星座: {lunar.starZodiac}",
        f"纳音: {lunar.get_nayin()}",
        f"彭祖百忌: {lunar.get_pengTaboo(delimit=', ')}",
        f"值日: {officer[0]}执位",
        f"值神: {officer[1]}({officer[2]})",
        f"廿八宿: {lunar.get_the28Stars()}",
        f"吉神方位: {' '.join(lunar.get_luckyGodsDirection())}",
        f"今日胎神: {lunar.get_fetalGod()}",
        f"宜: {'、'.join(lunar.goodThing[:10])}",
        f"忌: {'、'.join(lunar.badThing[:10])}",
        "(默认返回干支年和农历日期；仅在要求查询宜忌信息时才返回本日宜忌)",
    ]
    return "\n".join(lines)


@register_function("get_lunar", get_lunar_function_desc, ToolType.WAIT)
def get_lunar(date=None, query=None):
    """查询指定日期（默认今天）的农历与黄历信息。"""
    from fuguoai_core.utils.cache.manager import CacheType, cache_manager

    if date:
        try:
            now = datetime.strptime(date, _DATE_FORMAT)
        except ValueError:
            return ActionResponse(
                Action.REQLLM,
                "日期格式错误，请使用YYYY-MM-DD格式，例如：2024-01-01",
                None,
            )
    else:
        now = datetime.now(_BEIJING_TZ)

    current_date = now.strftime(_DATE_FORMAT)
    if query is None:
        query = _DEFAULT_QUERY

    cache_key = f"lunar_info_{current_date}"
    cached = cache_manager.get(CacheType.LUNAR, cache_key)
    if cached:
        return ActionResponse(Action.REQLLM, cached, None)

    response_text = _build_lunar_text(cnlunar.Lunar(now, godType="8char"), query)
    cache_manager.set(CacheType.LUNAR, cache_key, response_text)

    return ActionResponse(Action.REQLLM, response_text, None)
