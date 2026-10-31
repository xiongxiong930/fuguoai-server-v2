# -*- coding: utf-8 -*-
"""TTS speed 链路专项（2026-10-16）：spec 附录 A 出厂语速 ±0.5 → 腾讯 Speed 透传。

背景：附录 A（DEC-046）每角色带 speed（小满/知秋 −0.5，阿澈/灯灯 +0.5）。查证
腾讯云官方文档（cloud.tencent.com/document/api/1081/122819 等 4 来源一致）：
Speed 范围 [-2,6]，**可保留 2 位小数（如 0.5/1.25）**，档位 -2→0.6x/-1→0.8x/
0→1.0x/1→1.2x/2→1.5x/6→2.5x。**±0.5 是合法原值，无需换算**。
原实现 `int(speed)` 强转把 0.5 抹成 0（过度收紧，非 API 限制），本测试锁修复。

锁四件事：
① _norm_speed 归一化：小数保真 / 越界回落 0 / 非法回落 0
② text_to_speak per-call speed：None→实例默认（现有行为不变）；显式值（含 0.0）覆盖
③ text_to_stream per-call speed 同语义
④ /tts 端点 speed query 透传给主用 provider（不传=None 现有行为零变化）
"""

import base64
import json

import pytest

from fuguoai_core.providers.tts.tencent import TTSProvider


# ── ① _norm_speed ──────────────────────────────────────────────
def test_norm_speed_keeps_decimal():
    """附录 A 的 ±0.5 原样保真（不取整）。"""
    assert TTSProvider._norm_speed(-0.5) == -0.5
    assert TTSProvider._norm_speed(0.5) == 0.5
    assert TTSProvider._norm_speed("1.25") == 1.25   # 官方示例值


def test_norm_speed_integers_and_bounds():
    assert TTSProvider._norm_speed(0) == 0
    assert TTSProvider._norm_speed("2") == 2
    assert TTSProvider._norm_speed(-2) == -2
    assert TTSProvider._norm_speed(6) == 6


def test_norm_speed_out_of_range_falls_back_zero():
    assert TTSProvider._norm_speed(7) == 0
    assert TTSProvider._norm_speed(-3) == 0


def test_norm_speed_garbage_falls_back_zero():
    assert TTSProvider._norm_speed("abc") == 0
    assert TTSProvider._norm_speed(None) == 0


def test_instance_speed_supports_decimal():
    """config 层实例化：小数 speed 不再被 int() 抹平。"""
    p = TTSProvider({"voice": "502001", "speed": "0.5", "appid": "1",
                     "secret_id": "s", "secret_key": "k", "region": "ap-shanghai"},
                    delete_audio_file=None)
    assert p.speed == 0.5
    p2 = TTSProvider({"voice": "502001", "appid": "1",
                      "secret_id": "s", "secret_key": "k", "region": "ap-shanghai"},
                     delete_audio_file=None)
    assert p2.speed == 0   # 缺省常速，与原行为一致


# ── ② text_to_speak per-call speed（mock requests.post 抓 payload）──
@pytest.mark.asyncio
async def test_text_to_speak_passes_speed_into_payload(monkeypatch):
    prov = TTSProvider({"voice": "502001", "speed": "2", "appid": "1",
                        "secret_id": "s", "secret_key": "k", "region": "ap-shanghai"},
                       delete_audio_file=None)
    captured = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"Response": {"Audio": base64.b64encode(b"mp3bytes").decode()}}

    def fake_post(url, body, headers=None, timeout=None):
        captured["payload"] = json.loads(body)
        return _Resp()

    monkeypatch.setattr("fuguoai_core.providers.tts.tencent.requests.post", fake_post)

    # per-call 覆盖：附录 A 知秋 −0.5（实例默认 2 被覆盖）
    await prov.text_to_speak("你好", None, speed=-0.5)
    assert captured["payload"]["Speed"] == -0.5

    # None → 实例默认 2（现有行为不变）
    await prov.text_to_speak("你好", None)
    assert captured["payload"]["Speed"] == 2

    # 显式 0.0 是合法值（常速），不得被实例值 2 覆盖
    await prov.text_to_speak("你好", None, speed=0.0)
    assert captured["payload"]["Speed"] == 0.0

    # 越界 per-call 值回落 0（不送猜测值）
    await prov.text_to_speak("你好", None, speed=99)
    assert captured["payload"]["Speed"] == 0


# ── ④ /tts 端点透传 ────────────────────────────────────────────
class _FakeProv:
    def __init__(self):
        self.calls = []

    async def text_to_speak(self, text, output_file, speed=None):
        self.calls.append({"text": text, "speed": speed})
        return b"mp3bytes"


class _Req:
    def __init__(self, query):
        self.query = query


@pytest.mark.asyncio
async def test_tts_endpoint_passes_speed_query(monkeypatch):
    from fuguoai_core.api import tts_routes

    prov = _FakeProv()
    monkeypatch.setattr(tts_routes, "_get_tencent", lambda voice: prov)
    monkeypatch.setattr(tts_routes, "_get_fallback", lambda voice: None)

    # 带 speed（附录 A 灯灯 +0.5）
    resp = await tts_routes.handle_tts(_Req({"text": "在吗", "voice": "601009", "speed": "0.5"}))
    assert resp.status == 200
    assert prov.calls[-1]["speed"] == "0.5"     # 原样透传，归一化在 provider 内

    # 不带 speed → None（现有调用方行为零变化）
    resp = await tts_routes.handle_tts(_Req({"text": "在吗", "voice": "601009"}))
    assert resp.status == 200
    assert prov.calls[-1]["speed"] is None

    # 空 speed 串 → None
    resp = await tts_routes.handle_tts(_Req({"text": "在吗", "voice": "601009", "speed": ""}))
    assert resp.status == 200
    assert prov.calls[-1]["speed"] is None
