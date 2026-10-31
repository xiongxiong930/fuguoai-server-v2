# -*- coding: utf-8 -*-
"""设备面 TTS 音色闭环自进化测试（networked_reminder_scheduler per-voice 化）。

2026-10-16：角色设备面回复此前走 `_get_tts()` config 单例——**角色 VoiceType
在设备推送链路根本没用上**，盒子播任何角色都用 config 默认声线。本轮把
tts_routes 已在 159 生产验证的 per-voice 范式（private_voice 透传）搬进
调度器，并保证 voice=None 的提醒/播报路径行为零变化。

覆盖：
  ① _get_tts()：无 voice → 系统单例（原路径，缓存复用）
  ② _get_tts(voice)：per-voice 腾讯实例（private_voice/format 透传 + 缓存）
  ③ per-voice 构建失败 / config 无 TencentTTS 段 → None（调用方回落默认）
  ④ _tts_to_opus/_tts_to_opus_chat：voice 透传 + 合成失败回落默认声线
  ⑤ speak_text_to_device：voice 到达双推流路（chat 通道 / vm-server 回退）
"""

import asyncio

import pytest

import fuguoai_core.utils.networked_reminder_scheduler as nrs

# ⚠️ 沙箱坑（2026-10-16 实踩）：fuguoai_core.utils.tts / modules_initialize 是
# 调度器**惰性 import**（在 _get_tts 函数体内），测试若用 monkeypatch 字符串
# 路径 patch 它们，会在 pytest-asyncio 的事件循环内触发模块首 import →
# setup_logging()→check_config_file()→asyncio.run(load_config()) 撞运行中的
# loop 直接炸。故在收集期（无 loop）提前导入，让字符串 patch 只做属性替换。
import fuguoai_core.utils.modules_initialize  # noqa: F401
import fuguoai_core.utils.tts  # noqa: F401


class _FakeCacheManager:
    def __init__(self, cfg):
        self._cfg = cfg

    def get(self, ctype, key):
        return self._cfg


class _FakeProvider:
    """记录 text_to_speak 调用的假 provider。"""

    def __init__(self, name="prov", fail=False):
        self.name = name
        self.fail = fail
        self.calls = []
        self.audio_file_type = "mp3"

    async def text_to_speak(self, text, _none, **kw):
        self.calls.append(text)
        if self.fail:
            return None
        return b"\x00" * 64


@pytest.fixture
def clean_tts(monkeypatch):
    """清空两级 TTS 缓存，避免跨测试串实例。"""
    monkeypatch.setattr(nrs, "_TTS", None)
    monkeypatch.setattr(nrs, "_TTS_BY_VOICE", {})
    monkeypatch.setattr(nrs, "_speaking", set())
    return monkeypatch


def _patch_cache(monkeypatch, cfg):
    monkeypatch.setattr("fuguoai_core.utils.cache.manager.cache_manager",
                        _FakeCacheManager(cfg))


_TENCENT_CFG = {"TTS": {"TencentTTS": {"secret_id": "x", "secret_key": "y",
                                       "appid": "z"}}}


# ───────────────────────── ① 无 voice：系统单例 ─────────────────────────

@pytest.mark.asyncio
async def test_get_tts_none_voice_uses_system_singleton(clean_tts):
    """voice=None → initialize_tts(cfg) 单例，二次调用复用（提醒路径不变）。"""
    made = []

    def fake_init(cfg):
        made.append(cfg)
        return _FakeProvider("system")

    _patch_cache(clean_tts, {"selected_module": {"TTS": "EdgeTTS"}})
    clean_tts.setattr("fuguoai_core.utils.modules_initialize.initialize_tts", fake_init)

    p1 = nrs._get_tts()
    p2 = nrs._get_tts()
    assert p1 is p2 and p1.name == "system"
    assert len(made) == 1          # 只构建一次


# ───────────────────────── ② per-voice 实例 ─────────────────────────

@pytest.mark.asyncio
async def test_get_tts_voice_builds_per_voice_tencent(clean_tts):
    """voice=601011 → create_instance('tencent', block) 且 private_voice 透传。"""
    built = []

    def fake_create(ttype, block, _none):
        built.append((ttype, dict(block)))
        return _FakeProvider("tencent-601011")

    _patch_cache(clean_tts, _TENCENT_CFG)
    clean_tts.setattr("fuguoai_core.utils.tts.create_instance", fake_create)

    p1 = nrs._get_tts("601011")
    p2 = nrs._get_tts("601011")     # 缓存复用：只构建一次
    assert p1 is p2 and p1.name == "tencent-601011"
    assert len(built) == 1
    ttype, block = built[0]
    assert ttype == "tencent"
    assert block["private_voice"] == "601011"   # 关键：角色声线透传
    assert block["format"] == "mp3"             # 与 tts_routes 一致强制 mp3


@pytest.mark.asyncio
async def test_get_tts_voice_none_still_singleton_path(clean_tts):
    """空串 voice 按无 voice 处理（回落系统单例），不建 per-voice。"""
    _patch_cache(clean_tts, {"selected_module": {}})
    # cfg 缺 selected → initialize_tts 不被调 → 单例为 None，但也不许炸
    assert nrs._get_tts("") is None
    assert nrs._get_tts(None) is None


# ───────────────────────── ③ 构建失败回落 ─────────────────────────

@pytest.mark.asyncio
async def test_get_tts_voice_build_failure_returns_none(clean_tts):
    """per-voice 构建异常 → None（调用方回落默认声线，不静音）。"""
    def boom(ttype, block, _none):
        raise RuntimeError("no cred")

    _patch_cache(clean_tts, _TENCENT_CFG)
    clean_tts.setattr("fuguoai_core.utils.tts.create_instance", boom)
    assert nrs._get_tts("601011") is None


@pytest.mark.asyncio
async def test_get_tts_voice_without_tencent_config_returns_none(clean_tts):
    """config 无 TencentTTS 段 → None（不拿 Edge 段滥竽充数）。"""
    _patch_cache(clean_tts, {"TTS": {"EdgeTTS": {"voice": "zh-CN-XiaoxiaoNeural"}}})
    assert nrs._get_tts("601011") is None


# ───────────────────────── ④ _tts_to_opus 透传 + 回落 ─────────────────────────

@pytest.mark.asyncio
async def test_tts_to_opus_passes_voice(clean_tts):
    """voice 到达 _get_tts；per-voice provider 的合成结果被采用。"""
    seen = {}

    def fake_get_tts(voice=None):
        seen["voice"] = voice
        return _FakeProvider("tencent-601011")

    async def fake_synth(provider, text):
        return b"\x00" * 64, "mp3"

    clean_tts.setattr(nrs, "_get_tts", fake_get_tts)
    clean_tts.setattr(nrs, "_synth_with_fallback", fake_synth)
    clean_tts.setattr(nrs, "decode_to_pcm", lambda audio, fmt: b"\x00" * 320)
    clean_tts.setattr(nrs, "pcm_to_frames", lambda pcm: [b"f1", b"f2"])

    frames = await nrs._tts_to_opus("你好", voice="601011")
    assert frames == [b"f1", b"f2"]
    assert seen["voice"] == "601011"


@pytest.mark.asyncio
async def test_tts_to_opus_falls_back_when_per_voice_synth_fails(clean_tts):
    """per-voice 合成失败 → 回落系统默认单例再试（不静音）。"""
    calls = []

    def fake_get_tts(voice=None):
        calls.append(voice)
        return _FakeProvider(name=("per-voice" if voice else "system"),
                             fail=bool(voice))

    async def fake_synth(provider, text):
        if provider.fail:
            return None, "mp3"
        return b"\x00" * 64, "mp3"

    clean_tts.setattr(nrs, "_get_tts", fake_get_tts)
    clean_tts.setattr(nrs, "_synth_with_fallback", fake_synth)
    clean_tts.setattr(nrs, "decode_to_pcm", lambda audio, fmt: b"\x00" * 320)
    clean_tts.setattr(nrs, "pcm_to_frames", lambda pcm: [b"f1"])

    frames = await nrs._tts_to_opus("你好", voice="601011")
    assert frames == [b"f1"]                 # 默认声线兜底有声
    assert calls == ["601011", None]         # 先角色音色，后回落默认


@pytest.mark.asyncio
async def test_tts_to_opus_none_voice_unchanged(clean_tts):
    """voice=None → 只走系统单例一次，无回落重试（提醒路径零变化）。"""
    calls = []

    def fake_get_tts(voice=None):
        calls.append(voice)
        return _FakeProvider("system")

    async def fake_synth(provider, text):
        return b"\x00" * 64, "mp3"

    clean_tts.setattr(nrs, "_get_tts", fake_get_tts)
    clean_tts.setattr(nrs, "_synth_with_fallback", fake_synth)
    clean_tts.setattr(nrs, "decode_to_pcm", lambda audio, fmt: b"\x00" * 320)
    clean_tts.setattr(nrs, "pcm_to_frames", lambda pcm: [b"f1"])

    frames = await nrs._tts_to_opus("提醒内容")
    assert frames == [b"f1"]
    assert calls == [None]


# ───────────────────────── ⑤ speak_text_to_device 双路透传 ─────────────────────────

@pytest.mark.asyncio
async def test_speak_text_to_device_voice_reaches_chat_channel(clean_tts):
    """新固件（PlayMessage）路：voice 透传到 _speak_via_chat。"""
    seen = {}

    class _Conn:
        pass

    async def fake_via_chat(conn, device_id, text, label, voice=None):
        seen.update({"voice": voice, "text": text})
        return True

    clean_tts.setattr(nrs, "_get_conn", lambda did: _Conn())
    clean_tts.setattr(nrs, "_device_supports_playmessage", lambda conn: True)
    clean_tts.setattr(nrs, "_speak_via_chat", fake_via_chat)

    ok = await nrs.speak_text_to_device("box-1", "角色回复", voice="601011")
    assert ok is True
    assert seen["voice"] == "601011"


@pytest.mark.asyncio
async def test_speak_text_to_device_voice_reaches_vm_server_fallback(clean_tts):
    """老固件回退路：voice 透传到 _tts_to_opus。"""
    seen = {}

    async def fake_tts_to_opus(text, voice=None):
        seen.update({"voice": voice})
        return [b"f1"]

    async def fake_stream(device_id, frames, label=""):
        return True

    clean_tts.setattr(nrs, "_get_conn", lambda did: None)   # 无连接 → 回退路
    clean_tts.setattr(nrs, "_tts_to_opus", fake_tts_to_opus)
    clean_tts.setattr(nrs, "_stream_opus_to_device", fake_stream)

    ok = await nrs.speak_text_to_device("box-1", "角色回复", voice="603006")
    assert ok is True
    assert seen["voice"] == "603006"


@pytest.mark.asyncio
async def test_speak_text_to_device_none_voice_unchanged(clean_tts):
    """提醒/播报调用不传 voice → 双路都收 None（行为零变化）。"""
    seen = []

    class _Conn:
        pass

    async def fake_via_chat(conn, device_id, text, label, voice=None):
        seen.append(voice)
        return True

    clean_tts.setattr(nrs, "_get_conn", lambda did: _Conn())
    clean_tts.setattr(nrs, "_device_supports_playmessage", lambda conn: True)
    clean_tts.setattr(nrs, "_speak_via_chat", fake_via_chat)

    await nrs.speak_text_to_device("box-1", "到点提醒")
    assert seen == [None]
