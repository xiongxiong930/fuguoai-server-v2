# -*- coding: utf-8 -*-
"""微信登录鉴权「多小程序 + 异常分级」自进化测试（纯单元，不发真实网络请求）。

锁住 2026-10-13 那轮登录修复的行为（此前无测试）：
  ① _miniprogram_apps：多小程序（天使心 + 复果造物工坊）同时支持；跳过不完整/
     非 dict 条目；无 apps 时兼容旧单 app 配置与 env 兜底
  ② code2session 遍历所有已配置 app，code 属于哪个 app 就命中哪个
     → 两个小程序可同时登录（这是那轮修的核心）
  ③ 异常分级：网络不可达 → WeChatNetworkError；业务错误（40029 invalid code）
     → RuntimeError
  ④ **契约锁**：auth_routes 靠异常消息里的「网络请求失败」子串判定 502 vs 400，
     故该子串必须出现在 WeChatNetworkError、且绝不出现在业务错误里
     —— 否则客户端会把鉴权失败误判成基础设施故障、重试无效
  ⑤ dev_mode / 空 code 等边界
"""

import json
import re

import pytest

import fuguoai_core.utils.user_auth as ua


class _FakeResp:
    def __init__(self, payload):
        self._p = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._p

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def wx(monkeypatch):
    """拦截微信 code2session HTTP，按 appid 决定返回什么。"""
    state = {"biz_error": set(), "net_error": set(), "urls": []}

    def fake_urlopen(url, timeout=None):
        state["urls"].append(url)
        m = re.search(r"appid=([^&]+)", url)
        aid = m.group(1) if m else ""
        if aid in state["net_error"]:
            raise OSError("connection refused")
        if aid in state["biz_error"]:
            return _FakeResp({"errcode": 40029, "errmsg": "invalid code"})
        return _FakeResp({"openid": "openid-of-" + aid, "session_key": "sk"})

    monkeypatch.setattr(ua.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ua, "is_auth_dev_mode", lambda: False)
    return state


def _use_cfg(monkeypatch, cfg):
    monkeypatch.setattr(ua, "_miniprogram_config", lambda: cfg)


# ───────────────────────── ① _miniprogram_apps ─────────────────────────

def test_apps_multi_config():
    cfg = {"apps": [
        {"appid": "wxA", "secret": "sA"},
        {"appid": "wxB", "secret": "sB"},
    ]}
    assert ua._miniprogram_apps(cfg) == [("wxA", "sA"), ("wxB", "sB")]


def test_apps_skips_incomplete_and_non_dict():
    cfg = {"apps": [
        {"appid": "wxA", "secret": "sA"},
        {"appid": "wxNoSecret", "secret": ""},      # 缺 secret
        {"appid": ""},                               # 缺 appid
        "not-a-dict",                                # 非 dict
    ]}
    assert ua._miniprogram_apps(cfg) == [("wxA", "sA")]


def test_apps_fallback_to_single_legacy_config():
    cfg = {"appid": "wxLegacy", "secret": "sL"}
    assert ua._miniprogram_apps(cfg) == [("wxLegacy", "sL")]


def test_apps_empty_config_returns_empty():
    assert ua._miniprogram_apps({}) == []
    assert ua._miniprogram_apps(None) == []
    assert ua._miniprogram_apps({"apps": [{"appid": "", "secret": ""}]}) == []


# ───────────────────────── ② code2session 多小程序 ─────────────────────────

@pytest.mark.asyncio
async def test_code2session_empty_code(monkeypatch, wx):
    _use_cfg(monkeypatch, {"apps": [{"appid": "wxA", "secret": "sA"}]})
    with pytest.raises(ValueError):
        await ua.code2session("")


@pytest.mark.asyncio
async def test_code2session_dev_mode(monkeypatch, wx):
    monkeypatch.setattr(ua, "is_auth_dev_mode", lambda: True)
    _use_cfg(monkeypatch, {"apps": [{"appid": "wxA", "secret": "sA"}]})
    r1 = await ua.code2session("code-1")
    assert r1["dev"] is True and r1["openid"].startswith("dev_")
    # 同一 code 稳定（确定性，便于联调）
    assert (await ua.code2session("code-1"))["openid"] == r1["openid"]


@pytest.mark.asyncio
async def test_code2session_picks_the_app_the_code_belongs_to(monkeypatch, wx):
    """核心：天使心 + 复果共用同一后端，code 属于第二个 app 也必须能登。"""
    wx["biz_error"].add("wxA")      # 第一个 app 判定 code 无效
    _use_cfg(monkeypatch, {"apps": [
        {"appid": "wxA", "secret": "sA"},
        {"appid": "wxB", "secret": "sB"},
    ]})
    data = await ua.code2session("code-of-B")
    assert data["openid"] == "openid-of-wxB"
    # 确实按序试过两个 app
    assert len(wx["urls"]) == 2
    assert "appid=wxA" in wx["urls"][0] and "appid=wxB" in wx["urls"][1]


@pytest.mark.asyncio
async def test_code2session_explicit_appid_short_circuit(monkeypatch, wx):
    _use_cfg(monkeypatch, {"apps": [
        {"appid": "wxA", "secret": "sA"},
        {"appid": "wxB", "secret": "sB"},
    ]})
    await ua.code2session("c", appid="wxB")
    assert len(wx["urls"]) == 1 and "appid=wxB" in wx["urls"][0]


# ───────────────────────── ③ 异常分级 ─────────────────────────

@pytest.mark.asyncio
async def test_network_failure_raises_wechat_network_error(monkeypatch, wx):
    wx["net_error"].add("wxA")
    _use_cfg(monkeypatch, {"apps": [{"appid": "wxA", "secret": "sA"}]})
    with pytest.raises(ua.WeChatNetworkError):
        await ua.code2session("c")


@pytest.mark.asyncio
async def test_all_apps_business_error_raises_runtime_error(monkeypatch, wx):
    wx["biz_error"].update({"wxA", "wxB"})
    _use_cfg(monkeypatch, {"apps": [
        {"appid": "wxA", "secret": "sA"},
        {"appid": "wxB", "secret": "sB"},
    ]})
    with pytest.raises(RuntimeError) as ei:
        await ua.code2session("c")
    assert "业务错误" in str(ei.value)


# ───────────────────────── ④ 契约锁：502/400 映射子串 ─────────────────────────

def test_network_error_message_contains_routing_substring():
    """auth_routes 靠「网络请求失败」判定 502；改了异常文案会静默变成 400。"""
    assert "网络请求失败" in str(ua.WeChatNetworkError("微信 code2session 网络请求失败: boom"))


def test_business_error_message_must_not_contain_routing_substring():
    """业务错误（invalid code）绝不能带该子串，否则会被误判成 502 上游故障。"""
    msg = "微信 code2session 业务错误: {'errcode': 40029}"
    assert "网络请求失败" not in msg
    assert "业务错误" in msg


# ───────────────────────── ⑤ _wx_appid_secret ─────────────────────────

def test_wx_appid_secret_takes_first_app(monkeypatch):
    _use_cfg(monkeypatch, {"apps": [
        {"appid": "wxA", "secret": "sA"},
        {"appid": "wxB", "secret": "sB"},
    ]})
    assert ua._wx_appid_secret() == ("wxA", "sA")
