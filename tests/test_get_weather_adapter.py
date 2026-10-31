# -*- coding: utf-8 -*-
"""天气双 provider 适配层 离线测试（免 key 起步 + 可切换 + 混合形态 + 结构化接口）。

直接脚本式运行，无需 pytest。PYTHONPATH=REPO 由回归门禁注入。
所有用例均离线：fetch_wttr/fetch_qweather 用桩替换，不触网。
覆盖：wttr 代码映射、import os 修复、provider 切换、qweather 缺 key 降级、
      报告动态天数标题、默认 provider=wttr、归一化图标枚举双 provider 一致性、
      get_weather_struct 字段完整性、GET /api/v1/weather 设备鉴权（device-id / token）。

注意：fetch 系列元组签名已是 5 元 (city, abstract, basic, temps, raw_code)。
"""
import asyncio

from fuguoai_plugins.functions import get_weather as gw
from fuguoai_core.api import weather_routes as wr
from fuguoai_core.utils.config_holder import get_config as _get_config
from fuguoai_core.auth import AuthManager
from aiohttp.test_utils import make_mocked_request


def _check(name, cond):
    if cond:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name}")
        raise AssertionError(name)


def _fake_wttr(location, lang="zh"):
    # 五元组：(city, abstract, basic_dict, temps_list, raw_code)
    return ("广州", "阵雨", {"气温": "27℃", "体感": "30℃", "湿度": "86%", "风速": "15km/h"},
            [("2026-08-02", "阵雨", 25, 29)], "353")


def _fake_qweather(location, api_key, api_host, lang="zh"):
    return ("杭州市", "雷阵雨", {"湿度": "80%"},
            [("2026-08-02", "雷阵雨", "26", "33"), ("2026-08-03", "多云", "27", "34")], None)


def test_wttr_code_map_covers_seen_codes():
    _check("wttr code 353 -> 阵雨", gw.WTTR_CODE_MAP.get("353") == "阵雨")
    _check("wttr code 176 -> 零星小雨", gw.WTTR_CODE_MAP.get("176") == "零星小雨")
    _check("wttr code 113 -> 晴", gw.WTTR_CODE_MAP.get("113") == "晴")
    _check("wttr code 389 -> 雷阵雨", gw.WTTR_CODE_MAP.get("389") == "雷阵雨")


def test_module_imports_cleanly():
    # 复现此前潜伏 bug：缺 import os 会导致 os.getenv NameError。
    # 模块顶层已 import os，且 generate_weather_report 的 qweather 分支会用到 os.getenv；
    # 只要模块能 import、且 qweather 分支可调用，即证明 import os 已修复。
    import os as _os
    _check("模块导入后 os 可用（修复 import os 缺失）", _os.getenv("QWEATHER_API_KEY", "") is not None)


def test_wttr_provider_returns_report():
    orig = gw.fetch_wttr
    gw.fetch_wttr = _fake_wttr
    try:
        rep = gw.generate_weather_report("广州", "zh_CN", {"provider": "wttr"})
        _check("wttr provider 返回非空报告", bool(rep))
        _check("wttr 报告含城市名", "广州" in rep)
        _check("wttr 报告含当前天气", "当前天气: 阵雨" in rep)
        _check("wttr 报告动态天数标题=未来1天", "未来1天预报" in rep)
    finally:
        gw.fetch_wttr = orig


def test_qweather_missing_key_degrades_to_empty():
    orig_getenv = gw.os.getenv
    gw.os.getenv = lambda *a, **k: ""  # 强制无环境变量 key
    try:
        rep = gw.generate_weather_report("杭州", "zh_CN", {"provider": "qweather", "api_key": ""})
        _check("qweather 无 key 时优雅降级为空串", rep == "")
    finally:
        gw.os.getenv = orig_getenv


def test_qweather_provider_uses_fetch_qweather():
    orig = gw.fetch_qweather
    gw.fetch_qweather = _fake_qweather
    try:
        rep = gw.generate_weather_report(
            "杭州", "zh_CN", {"provider": "qweather", "api_key": "dummy", "api_host": "h"}
        )
        _check("qweather provider 返回非空报告", bool(rep))
        _check("qweather 报告含城市名", "杭州市" in rep)
        _check("qweather 报告动态天数标题=未来2天", "未来2天预报" in rep)
        _check("qweather 报告含气温区间", "26~33" in rep)
    finally:
        gw.fetch_qweather = orig


def test_default_provider_is_wttr():
    # config 未显式给 provider 时默认 wttr（免 key）
    orig = gw.fetch_wttr
    gw.fetch_wttr = _fake_wttr
    try:
        rep = gw.generate_weather_report("广州", "zh_CN", {})
        _check("缺省 provider 走 wttr 且返回报告", bool(rep))
    finally:
        gw.fetch_wttr = orig


def test_build_report_day_count_title():
    r3 = gw._build_report("X", "晴", {}, [("d1", "晴", 20, 30), ("d2", "多云", 21, 31), ("d3", "雨", 19, 28)])
    _check("3天 -> 未来3天预报标题", "未来3天预报" in r3)
    r7 = gw._build_report("X", "晴", {}, [("d%d" % i, "晴", 20, 30) for i in range(7)])
    _check("7天 -> 未来7天预报标题", "未来7天预报" in r7)


# ---------------------------------------------------------------------------
# 归一化图标枚举（固件只认这组，后端切数据源无感）
# ---------------------------------------------------------------------------
def test_normalized_icon_maps_consistency():
    # 两套原始代码映射的值域都必须落在归一化枚举内
    for code, icon in gw.WTTR_ICON_MAP.items():
        _check(f"wttr {code} -> 归一化枚举 {icon}", icon in gw.NORMALIZED_WEATHER_ICONS)
    for code, icon in gw.QWEATHER_ICON_MAP.items():
        _check(f"qweather {code} -> 归一化枚举 {icon}", icon in gw.NORMALIZED_WEATHER_ICONS)
    # 关键一致性：同一"晴"两套体系都映射到 sunny；"多云"都映射到 cloudy
    _check("wttr 113(晴) -> sunny", gw.WTTR_ICON_MAP.get("113") == "sunny")
    _check("qweather 100(晴) -> sunny", gw.QWEATHER_ICON_MAP.get("100") == "sunny")
    _check("wttr 116(多云) -> cloudy", gw.WTTR_ICON_MAP.get("116") == "cloudy")
    _check("qweather 101(多云) -> cloudy", gw.QWEATHER_ICON_MAP.get("101") == "cloudy")


def test_get_weather_struct_fields():
    orig = gw.fetch_wttr
    gw.fetch_wttr = _fake_wttr
    try:
        data = asyncio.run(gw.get_weather_struct("广州", {"provider": "wttr"}, client_ip=None))
        _check("返回 dict 非空", isinstance(data, dict))
        for k in ("city", "icon", "desc", "temp", "high", "low", "humidity", "provider", "ts"):
            _check(f"含字段 {k}", k in data)
        _check("city=广州", data["city"] == "广州")
        _check("icon=rain (wttr 353)", data["icon"] == "rain")
        _check("desc=阵雨", data["desc"] == "阵雨")
        _check("temp=27", data["temp"] == 27)
        _check("high=29 (int)", data["high"] == 29 and isinstance(data["high"], int))
        _check("low=25 (int)", data["low"] == 25 and isinstance(data["low"], int))
        _check("humidity=86", data["humidity"] == 86)
        _check("provider=wttr", data["provider"] == "wttr")
        _check("ts 为 int 时间戳", isinstance(data["ts"], int))
    finally:
        gw.fetch_wttr = orig


def test_get_weather_struct_qweather_desc_fallback():
    # 和风当前码未解析(raw_code=None)时，用中文描述兜底到归一化枚举
    orig = gw.fetch_qweather
    gw.fetch_qweather = _fake_qweather
    try:
        data = asyncio.run(gw.get_weather_struct("杭州", {"provider": "qweather", "api_key": "x"}, client_ip=None))
        _check("qweather desc=雷阵雨", data["desc"] == "雷阵雨")
        _check("icon=storm (desc 兜底)", data["icon"] == "storm")
        _check("high/low 字符串转 int", data["high"] == 33 and data["low"] == 26)
        _check("provider=qweather", data["provider"] == "qweather")
    finally:
        gw.fetch_qweather = orig


# ---------------------------------------------------------------------------
# GET /api/v1/weather 设备鉴权（与 OTA 同语义，不用 api_guard）
# ---------------------------------------------------------------------------
def _run(req):
    resp = asyncio.run(wr.handle_weather(req))
    body = __import__("json").loads(resp.body.decode("utf-8"))
    return resp.status, body


def _set_auth(enabled, auth_key="secret", whitelist=None):
    cfg = _get_config()
    saved = dict(cfg)
    cfg.clear()
    # 真实配置结构：auth_key 与 auth 并列在 server 下（与 ota_handler 一致）
    cfg["server"] = {
        "auth": {"enabled": enabled, "allowed_devices": whitelist or []},
        "auth_key": auth_key,
    }
    return saved


def _restore_auth(saved):
    cfg = _get_config()
    cfg.clear()
    cfg.update(saved)


def test_route_requires_device_id():
    req = make_mocked_request("GET", "/api/v1/weather")
    status, body = _run(req)
    _check("缺 device-id 返回 400", status == 400)
    _check("400 含错误码字段", body.get("code") == 400)


def test_route_auth_disabled_allows_with_device_id():
    saved = _set_auth(False)
    orig = gw.fetch_wttr
    gw.fetch_wttr = _fake_wttr
    wr._STRUCT_CACHE.clear()
    try:
        req = make_mocked_request(
            "GET", "/api/v1/weather?location=广州",
            headers={"device-id": "AA:BB:CC"},
        )
        status, body = _run(req)
        _check("auth 关闭 + 有 device-id -> 200", status == 200)
        _check("200 含 ok=true", body.get("ok") is True)
        _check("data.icon=rain", body.get("data", {}).get("icon") == "rain")
    finally:
        gw.fetch_wttr = orig
        wr._STRUCT_CACHE.clear()
        _restore_auth(saved)


def test_route_auth_enabled_rejects_without_token():
    saved = _set_auth(True, auth_key="secret")
    try:
        req = make_mocked_request(
            "GET", "/api/v1/weather?location=广州",
            headers={"device-id": "AA:BB:CC"},  # 无 token
        )
        status, body = _run(req)
        _check("auth 开启 + 无 token -> 401", status == 401)
    finally:
        _restore_auth(saved)


def test_route_auth_enabled_accepts_valid_token():
    saved = _set_auth(True, auth_key="secret")
    orig = gw.fetch_wttr
    gw.fetch_wttr = _fake_wttr
    wr._STRUCT_CACHE.clear()
    try:
        device_id, client_id = "AA:BB:CC", "cli1"
        token = AuthManager(secret_key="secret").generate_token(client_id, device_id)
        req = make_mocked_request(
            "GET", "/api/v1/weather?location=广州",
            headers={
                "device-id": device_id,
                "client-id": client_id,
                "Authorization": "Bearer " + token,
            },
        )
        status, body = _run(req)
        _check("auth 开启 + 合法 token -> 200", status == 200)
        _check("合法 token 返回 icon=rain", body.get("data", {}).get("icon") == "rain")
    finally:
        gw.fetch_wttr = orig
        wr._STRUCT_CACHE.clear()
        _restore_auth(saved)


def test_route_auth_enabled_whitelist_bypass():
    saved = _set_auth(True, auth_key="secret", whitelist=["AA:BB:CC"])
    orig = gw.fetch_wttr
    gw.fetch_wttr = _fake_wttr
    wr._STRUCT_CACHE.clear()
    try:
        # 白名单设备无需 token
        req = make_mocked_request(
            "GET", "/api/v1/weather?location=广州",
            headers={"device-id": "AA:BB:CC"},
        )
        status, body = _run(req)
        _check("白名单设备免 token -> 200", status == 200)
    finally:
        gw.fetch_wttr = orig
        wr._STRUCT_CACHE.clear()
        _restore_auth(saved)


if __name__ == "__main__":
    test_wttr_code_map_covers_seen_codes()
    test_module_imports_cleanly()
    test_wttr_provider_returns_report()
    test_qweather_missing_key_degrades_to_empty()
    test_qweather_provider_uses_fetch_qweather()
    test_default_provider_is_wttr()
    test_build_report_day_count_title()
    test_normalized_icon_maps_consistency()
    test_get_weather_struct_fields()
    test_get_weather_struct_qweather_desc_fallback()
    test_route_requires_device_id()
    test_route_auth_disabled_allows_with_device_id()
    test_route_auth_enabled_rejects_without_token()
    test_route_auth_enabled_accepts_valid_token()
    test_route_auth_enabled_whitelist_bypass()
    print("\nALL PASS ✅")
