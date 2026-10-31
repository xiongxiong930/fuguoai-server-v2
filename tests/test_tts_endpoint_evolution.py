# -*- coding: utf-8 -*-
"""天使心 app 面 TTS 端点自进化测试（纯单元，不依赖运行服务器 / 不发真实请求）。

锁住 2026-10-13 那轮 TTS 502 修复的行为（此前无测试）：
  ① _build_provider 对腾讯云强制注入 format=mp3（否则字节是 WAV 而响应头却是
     audio/mpeg，小程序播放器解不出），且不污染原 config 块
  ② 未配置的 provider 名 → None；非腾讯云不注入 format
  ③ handle_tts：missing text → 400；主用成功 → 200 + audio/mpeg
  ④ 主用失败 → 走 EdgeTTS 兜底；主用+兜底全失败 → 502（超时→504）
  ⑤ 失败响应只给通用文案，**不向客户端泄露内部异常细节**
  ⑥ /voices 返回音色列表
"""

import pytest

import fuguoai_core.api.tts_routes as tr


class _Req:
    def __init__(self, **query):
        self.query = query


@pytest.fixture(autouse=True)
def reset_caches(monkeypatch):
    """_PRIMARY / _FALLBACK_CACHE / _TENCENT_CACHE 是模块级单例，每个用例必须重置。"""
    monkeypatch.setattr(tr, "_PRIMARY", None)
    monkeypatch.setattr(tr, "_FALLBACK_CACHE", {})
    monkeypatch.setattr(tr, "_TENCENT_CACHE", {})


class _Prov:
    def __init__(self, result=None, exc=None):
        self._result = result
        self._exc = exc
        self.calls = []

    async def text_to_speak(self, text, _):
        self.calls.append(text)
        if self._exc:
            raise self._exc
        return self._result


# ───────────────────────── ① _build_provider ─────────────────────────

def test_build_tencent_forces_mp3_and_does_not_mutate_config(monkeypatch):
    block = {"type": "tencent", "appid": "1", "secret_id": "s", "secret_key": "k"}
    cfg = {"TTS": {"TencentTTS": block}}
    monkeypatch.setattr(tr, "get_config", lambda: cfg)

    seen = {}

    def fake_create(tts_type, b, _):
        seen["type"] = tts_type
        seen["block"] = b
        return "PROV"

    monkeypatch.setattr(tr, "create_instance", fake_create)

    assert tr._build_provider("TencentTTS") == "PROV"
    # 关键修复：强制 mp3，与 audio/mpeg 响应头一致
    assert seen["block"]["format"] == "mp3"
    # 只改端点局部副本，绝不污染全局 config
    assert "format" not in block


def test_build_provider_unknown_name_returns_none(monkeypatch):
    monkeypatch.setattr(tr, "get_config", lambda: {"TTS": {}})
    assert tr._build_provider("TencentTTS") is None
    assert tr._build_provider("NoSuchTTS") is None


def test_build_provider_non_tencent_no_format_injection(monkeypatch):
    monkeypatch.setattr(tr, "get_config",
                        lambda: {"TTS": {"EdgeTTS": {"type": "edge", "voice": "v"}}})
    seen = {}

    def fake_create(tts_type, b, _):
        seen["block"] = b
        return "P"

    monkeypatch.setattr(tr, "create_instance", fake_create)
    tr._build_provider("EdgeTTS")
    assert "format" not in seen["block"]


# ───────────────────────── ② 主用选择 ─────────────────────────

def test_primary_is_tencent_not_selected_module(monkeypatch):
    """主用固定腾讯云，不随 selected_module.TTS（159 仍是 EdgeTTS）切换，
    避免把设备/对讲语音链路也拖下水。"""
    built = []

    def fake_create(tts_type, b, _):
        built.append(tts_type)
        return "P"

    monkeypatch.setattr(tr, "get_config", lambda: {
        "TTS": {"TencentTTS": {"type": "tencent"}},
        "selected_module": {"TTS": "EdgeTTS"},
    })
    monkeypatch.setattr(tr, "create_instance", fake_create)

    assert tr._get_primary() == "P"
    assert built == ["tencent"]          # 没去建 EdgeTTS


def test_primary_falls_back_to_selected_module_when_tencent_absent(monkeypatch):
    built = []

    monkeypatch.setattr(tr, "get_config", lambda: {
        "TTS": {"EdgeTTS": {"type": "edge"}},
        "selected_module": {"TTS": "EdgeTTS"},
    })
    monkeypatch.setattr(tr, "create_instance", lambda t, b, _: (built.append(t) or "P"))

    assert tr._get_primary() == "P"
    assert built == ["edge"]


# ─────────── ②b per-request voice 透传（角色音色区分） ───────────

def test_get_tencent_honors_voice_and_caches(monkeypatch):
    """_get_tencent(voice) 把请求声线透传进 private_voice，并按 voice 缓存单例。"""
    built = []

    def fake_create(tts_type, b, _):
        built.append((tts_type, b.get("private_voice")))
        return "P"

    monkeypatch.setattr(tr, "get_config", lambda: {
        "TTS": {"TencentTTS": {"type": "tencent", "appid": "1",
                               "secret_id": "s", "secret_key": "k"}},
    })
    monkeypatch.setattr(tr, "create_instance", fake_create)

    # 不同 voice → 分别构建，private_voice 正确透传
    assert tr._get_tencent("1003") == "P"
    assert tr._get_tencent("1018") == "P"
    assert built == [("tencent", "1003"), ("tencent", "1018")]
    # 同 voice 再次请求 → 命中缓存，不再重建
    assert tr._get_tencent("1003") == "P"
    assert built == [("tencent", "1003"), ("tencent", "1018")]


def test_get_tencent_empty_voice_falls_back_to_primary(monkeypatch):
    """voice 为空/默认时回退 _get_primary（config 固定声线），不建 per-voice。"""
    monkeypatch.setattr(tr, "_get_primary", lambda: "PRIMARY")
    assert tr._get_tencent("") == "PRIMARY"
    assert tr._get_tencent(None) == "PRIMARY"


@pytest.mark.asyncio
async def test_tts_passes_request_voice_to_provider(monkeypatch):
    seen = {}

    def fake_tencent(voice):
        seen["voice"] = voice
        return _Prov(result=b"mp3-bytes")

    monkeypatch.setattr(tr, "_get_tencent", fake_tencent)
    monkeypatch.setattr(tr, "_get_fallback", lambda voice: None)

    resp = await tr.handle_tts(_Req(text="你好", voice="1003"))
    assert resp.status == 200
    assert seen["voice"] == "1003"
    assert resp.body == b"mp3-bytes"


# ───────────────────────── ③ handle_tts ─────────────────────────

@pytest.mark.asyncio
async def test_tts_missing_text_400():
    resp = await tr.handle_tts(_Req(text="", voice="v"))
    assert resp.status == 400


@pytest.mark.asyncio
async def test_tts_primary_success_returns_mp3(monkeypatch):
    prov = _Prov(result=b"ID3fake-mp3")
    monkeypatch.setattr(tr, "_get_tencent", lambda voice: prov)
    monkeypatch.setattr(tr, "_get_fallback", lambda voice: None)

    resp = await tr.handle_tts(_Req(text="你好"))
    assert resp.status == 200
    assert resp.content_type == "audio/mpeg"
    assert resp.body == b"ID3fake-mp3"
    assert prov.calls == ["你好"]


@pytest.mark.asyncio
async def test_tts_falls_back_when_primary_fails(monkeypatch):
    primary = _Prov(exc=RuntimeError("403 forbidden"))
    fallback = _Prov(result=b"fallback-mp3")
    monkeypatch.setattr(tr, "_get_tencent", lambda voice: primary)
    monkeypatch.setattr(tr, "_get_fallback", lambda voice: fallback)

    resp = await tr.handle_tts(_Req(text="你好"))
    assert resp.status == 200
    assert resp.body == b"fallback-mp3"
    assert fallback.calls == ["你好"]


@pytest.mark.asyncio
async def test_tts_all_fail_returns_502_without_leaking_reason(monkeypatch):
    monkeypatch.setattr(tr, "_get_tencent", lambda voice: _Prov(exc=RuntimeError("secret_key 无效")))
    monkeypatch.setattr(tr, "_get_fallback", lambda voice: _Prov(exc=RuntimeError("403")))

    resp = await tr.handle_tts(_Req(text="你好"))
    assert resp.status == 502
    body = resp.body.decode("utf-8")
    # 通用文案，绝不把内部异常（含密钥相关字眼）透给客户端
    assert "secret_key" not in body
    assert "403" not in body
    # 注：aiohttp json_response 默认转义非 ASCII，故按 JSON 解析后比对原文
    import json

    assert json.loads(body)["msg"] == "TTS 合成失败，请稍后重试"


@pytest.mark.asyncio
async def test_tts_timeout_maps_to_504(monkeypatch):
    monkeypatch.setattr(tr, "_get_tencent", lambda voice: _Prov(exc=RuntimeError("请求超时")))
    monkeypatch.setattr(tr, "_get_fallback", lambda voice: None)

    resp = await tr.handle_tts(_Req(text="你好"))
    assert resp.status == 504


@pytest.mark.asyncio
async def test_tts_no_provider_502(monkeypatch):
    monkeypatch.setattr(tr, "_get_tencent", lambda voice: None)
    monkeypatch.setattr(tr, "_get_fallback", lambda voice: None)

    resp = await tr.handle_tts(_Req(text="你好"))
    assert resp.status == 502


# ───────────────────────── ④ /voices ─────────────────────────

@pytest.mark.asyncio
async def test_voices_lists_known_voices():
    import json

    resp = await tr.handle_voices(_Req())
    data = json.loads(resp.body.decode("utf-8"))
    assert data["ok"] is True
    ids = [v["id"] for v in data["voices"]]
    assert "zh-CN-XiaoxiaoNeural" in ids
    assert len(ids) >= 5
