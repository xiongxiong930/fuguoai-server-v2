"""时间信息聚合（复果原生实现，clean-room 重写）。

向对话链路统一提供「当前时间 / 日期 / 星期 / 农历」四类信息，供 LLM 在回答中
引用真实时间。所有取值锚定北京时间（UTC+8），与复果面向中国大陆用户的定位一致。
"""

import cnlunar
from datetime import datetime, timezone, timedelta

_WEEK = {
    "Monday": "星期一",
    "Tuesday": "星期二",
    "Wednesday": "星期三",
    "Thursday": "星期四",
    "Friday": "星期五",
    "Saturday": "星期六",
    "Sunday": "星期日",
}
_TZ = timezone(timedelta(hours=8))


def _now():
    """返回锚定北京时间的当前时刻。"""
    return datetime.now(_TZ)


def _weekday_cn(now):
    """把英文星期名映射为中文（未知则回退「未知」）。"""
    return _WEEK.get(now.strftime("%A"), "未知")


def _lunar_cn(now):
    """把当前时刻换算为中文农历串；农历库不可用时回退「农历获取失败」。"""
    try:
        cal = cnlunar.Lunar(now, godType="8char")
        return "%s年%s%s" % (
            cal.lunarYearCn,
            cal.lunarMonthCn[:-1],
            cal.lunarDayCn,
        )
    except Exception:
        return "农历获取失败"


def get_current_time() -> str:
    """当前时间，格式 HH:MM（24 小时制）。"""
    return _now().strftime("%H:%M")


def get_current_date() -> str:
    """今天的日期，格式 YYYY-MM-DD。"""
    return _now().strftime("%Y-%m-%d")


def get_current_weekday() -> str:
    """今天是星期几（中文，如「星期三」）。"""
    return _weekday_cn(_now())


def get_current_lunar_date() -> str:
    """今天的农历日期。"""
    return _lunar_cn(_now())


def get_current_time_info() -> tuple:
    """聚合上述四类时间信息。

    返回：(当前时间字符串, 今天日期, 今天星期, 农历日期)。
    """
    now = _now()
    return (
        now.strftime("%H:%M"),
        now.strftime("%Y-%m-%d"),
        _weekday_cn(now),
        _lunar_cn(now),
    )
