# -*- coding: utf-8 -*-
"""天使心「activate / messages / session」端点自进化测试（纯单元，不依赖运行服务器）。

补齐自进化方向 A1 里剩下的零覆盖路由。重点锁两处**安全语义**而非 happy path：
  ① /activate：owned 未持卡 uid **手动选也进不去**（否则可绕过 GET /characters 分层）
  ② /messages：未激活时与 /activate 同闸门，不能靠「直接发消息」绕进来
另覆盖：app 面设备静音（device_should_speak=False）、content 必填 400、
LLM 未就绪 503、无 system_prompt 422、记忆分层 tier/hint、
session 读取与清空。
"""

import json

import pytest

from fuguoai_core.utils.character_store import (
    CHANNEL_APP,
    CHANNEL_DEVICE,
    CharacterSession,
    DeliveryTarget,
    TIER_PRESET,
    TIER_OWNED,
)


class _Profile:
    def __init__(self, cid, tier, name, voice_type="502001", speed=-0.5,
                 transition_line="……好像睡了很久。"):
        self.character_id = cid
        self.tier = tier
        self.name = name
        self.system_prompt = "SP-" + cid
        # 与真实 CharacterProfile 契约一致（character_store.py:91-99）
        self.voice_type = voice_type
        self.speed = speed
        self.transition_line = transition_line

    def to_dict(self):
        return {"character_id": self.character_id, "name": self.name,
                "tier": self.tier, "system_prompt": self.system_prompt,
                "voice_type": self.voice_type, "speed": self.speed,
                "transition_line": self.transition_line}


class _Profiles:
    def __init__(self, ps):
        self._m = {p.character_id: p for p in ps}

    def get(self, cid):
        return self._m.get(cid)

    def list_all(self):
        return list(self._m.values())

    def validate_id(self, cid):
        return cid in self._m


class _Sessions:
    """内存版会话存储（与真实 store 同语义：set_active / clear / get）。"""

    def __init__(self):
        self._db = {}
        self.cleared = []

    def get(self, uid):
        return self._db.get(uid)

    def set_active(self, uid, character_id, channel=None, device_id=None, now=0.0):
        s = self._db.get(uid) or CharacterSession(uid=uid)
        s.character_id = character_id
        if channel is not None:
            s.channel = channel
        if device_id is not None:
            s.device_id = device_id
        self._db[uid] = s
        return s

    def set_channel(self, uid, channel, now=0.0):
        s = self._db.get(uid)
        if not s:
            return None
        s.channel = channel
        return s

    def clear(self, uid):
        self.cleared.append(uid)
        return self._db.pop(uid, None) is not None


class _Router:
    def __init__(self, profiles):
        self.profiles = _Profiles(profiles)
        self.sessions = _Sessions()
        self.system_prompt = "SP"      # 置 None 可测 422 分支
        self.app_opens = []

    def on_app_open(self, uid, character_id, now=0.0):
        self.app_opens.append((uid, character_id))
        if not self.profiles.validate_id(character_id):
            return {"ok": False, "reason": "unknown_character"}
        p = self.profiles.get(character_id)
        self.sessions.set_active(uid, character_id, channel=CHANNEL_APP)
        return {"ok": True, "character_id": character_id, "name": p.name,
                "channel": CHANNEL_APP, "device_should_speak": False}

    def build_system_prompt(self, uid):
        return self.system_prompt

    def route_response(self, uid, text):
        sess = self.sessions.get(uid)
        if not sess or not sess.character_id:
            return DeliveryTarget(target="none")
        return DeliveryTarget(target=sess.channel, character_id=sess.character_id,
                              name=sess.character_id, text=text, voice="voice-test")


class _Registry:
    """user-1 持有 yaoye-004；mojito-005 无人持有。"""

    def owns(self, uid, character_id):
        return character_id == "yaoye-004" and uid == "user-1"


class _Req:
    def __init__(self, body=None, **match_info):
        self.match_info = match_info
        self._body = body or {}

    async def json(self):
        return self._body


def _payload(resp):
    return json.loads(resp.body.decode("utf-8"))


@pytest.fixture
def env(monkeypatch):
    import fuguoai_core.api.character_routes as cr
    from fuguoai_core.utils import character_memory

    profiles = [
        _Profile("chuxue-001", TIER_PRESET, "初雪"),   # preset：人人可用
        _Profile("yaoye-004", TIER_OWNED, "曜夜"),     # owned：user-1 持有
        _Profile("mojito-005", TIER_OWNED, "莫吉托"),  # owned：无人持有 → 闸门拦
    ]
    router = _Router(profiles)
    history = {"pushed": [], "cleared": []}

    async def fake_auth(request):
        return "user-1", None

    monkeypatch.setattr(cr, "_auth", fake_auth)
    monkeypatch.setattr(cr, "get_character_router", lambda: router)
    monkeypatch.setattr(cr, "get_card_registry", lambda: _Registry())
    monkeypatch.setattr(cr, "_apply_user_overrides", lambda uid, d: None)
    # 调校覆盖层（2026-10-16 音色闭环）：fake store 默认空 preferences，
    # 测试可经 env.user_store.prefs 注入 character_overrides；
    # 不 mock 真实 store 是为避免沙箱 data 目录读不到时的隐性依赖。
    class _FakeUserStore:
        def __init__(self):
            self.prefs = {}

        def get_preferences(self, uid):
            return dict(self.prefs.get(uid) or {})

        def clear_character_override(self, uid, cid):
            ov = (self.prefs.get(uid) or {}).get("character_overrides") or {}
            return ov.pop(cid, None) is not None

    user_store = _FakeUserStore()
    monkeypatch.setattr(cr, "get_user_store", lambda: user_store)
    monkeypatch.setattr(cr, "_get_history", lambda uid, cid: [])
    monkeypatch.setattr(
        cr, "_push_history",
        lambda uid, cid, role, content: history["pushed"].append((cid, role, content)))
    monkeypatch.setattr(
        cr, "_clear_history",
        lambda uid, cid=None: history["cleared"].append((uid, cid)))
    # 记忆分层：无卡=短期（不注入长期、回 hint），仅持卡才长期
    monkeypatch.setattr(character_memory, "tier_of", lambda uid, cid: character_memory.TIER_SHORT)
    monkeypatch.setattr(character_memory, "apply_to_system_prompt",
                        lambda sp, uid, cid, query="": (sp, ""))
    monkeypatch.setattr(character_memory, "extract_async",
                        lambda *a, **k: None)
    return type("E", (), {"cr": cr, "router": router, "history": history,
                          "cm": character_memory, "user_store": user_store})()


@pytest.fixture
def llm_ok(monkeypatch, env):
    class _LLM:
        def response_no_stream(self, system_prompt, content):
            return "回复:" + content

        def response(self, session_id, dialogue, **kwargs):
            # 与真实 openai provider 契约一致：同步生成器，dialogue 末条为本轮 user
            yield "回复:" + dialogue[-1]["content"]

    monkeypatch.setattr(env.cr, "_get_llm", lambda: _LLM())
    return env


# ───────────────────────── /activate ─────────────────────────

@pytest.mark.asyncio
async def test_activate_preset_ok_and_app_channel(env):
    """小程序点开角色 → app 面，设备结构性静音。"""
    resp = await env.cr.handle_activate(_Req(character_id="chuxue-001"))
    assert resp.status == 200
    d = _payload(resp)["data"]
    assert d["character_id"] == "chuxue-001"
    assert d["channel"] == CHANNEL_APP
    assert d["device_should_speak"] is False


@pytest.mark.asyncio
async def test_activate_owned_not_held_rejected(env):
    """核心闸门：owned 未持卡，手动选也进不去（否则绕过目录分层）。"""
    resp = await env.cr.handle_activate(_Req(character_id="mojito-005"))
    assert resp.status == 404
    assert _payload(resp)["message"] == "unknown_character"


@pytest.mark.asyncio
async def test_activate_returns_transition_line_and_tts(env):
    """契约 2.2 收敛：activate 附带过渡自语 + tts 参数（voice_type/speed/sample_rate）。"""
    resp = await env.cr.handle_activate(_Req(character_id="chuxue-001"))
    assert resp.status == 200
    d = _payload(resp)["data"]
    assert d["transition_line"] == "……好像睡了很久。"
    assert d["tts"]["voice_type"] == "502001"
    assert d["tts"]["speed"] == -0.5
    # HTTP 面（无设备连接）恒 16000；设备面 5xxxxx/6xxxxx 才 24k
    assert d["tts"]["sample_rate"] == 16000


@pytest.mark.asyncio
async def test_activate_speed_null_when_profile_unset(env):
    """未配置 speed 的角色：tts.speed=null，前端按「用默认」处理。"""
    env.router.profiles._m["chuxue-001"].speed = None
    resp = await env.cr.handle_activate(_Req(character_id="chuxue-001"))
    assert resp.status == 200
    d = _payload(resp)["data"]
    assert d["tts"]["speed"] is None
    assert d["tts"]["voice_type"] == "502001"


@pytest.mark.asyncio
async def test_activate_ritual_type_first_when_no_history(env):
    """契约 2.2 出参必填：无对话历史 → ritual_type=first（首次激活走完整仪式）。"""
    resp = await env.cr.handle_activate(_Req(character_id="chuxue-001"))
    assert resp.status == 200
    assert _payload(resp)["data"]["ritual_type"] == "first"


@pytest.mark.asyncio
async def test_activate_ritual_type_wake_when_history_exists(env, monkeypatch):
    """契约 2.2：有对话历史 → ritual_type=wake（回归唤醒）。

    旧注释误读契约称「first/wake 由设备端判定」——契约原文 first/wake 归
    后端出参，设备端只按 DEC-038 细分「换卡 vs 同卡重插」的表现。
    """
    monkeypatch.setattr(env.cr, "_get_history",
                        lambda uid, cid: [{"role": "user", "content": "你好"}])
    resp = await env.cr.handle_activate(_Req(character_id="chuxue-001"))
    assert resp.status == 200
    assert _payload(resp)["data"]["ritual_type"] == "wake"


@pytest.mark.asyncio
async def test_activate_locked_does_not_leak_tts(env):
    """闸门拦下的角色 404，响应体不得带任何 tts/transition_line/ritual_type 字段。"""
    resp = await env.cr.handle_activate(_Req(character_id="mojito-005"))
    assert resp.status == 404
    body = _payload(resp)
    assert "tts" not in body.get("data", {})
    assert "transition_line" not in body.get("data", {})
    assert "ritual_type" not in body.get("data", {})
    assert env.router.app_opens == []      # 根本没走到激活


@pytest.mark.asyncio
async def test_activate_owned_held_ok(env):
    resp = await env.cr.handle_activate(_Req(character_id="yaoye-004"))
    assert resp.status == 200
    assert _payload(resp)["data"]["name"] == "曜夜"


@pytest.mark.asyncio
async def test_activate_forces_app_even_if_device_declared(env):
    """客户端自报 source=device 也强制按 app 处理（小程序点开必为 app 面）。"""
    resp = await env.cr.handle_activate(
        _Req(body={"source": CHANNEL_DEVICE}, character_id="chuxue-001"))
    assert _payload(resp)["data"]["channel"] == CHANNEL_APP


@pytest.mark.asyncio
async def test_activate_tts_voice_type_uses_user_tuning(env):
    """契约 §2.3：activate tts.voice_type 取用户调校音色（voice_type 键）。"""
    env.user_store.prefs["user-1"] = {"character_overrides": {
        "chuxue-001": {"voice_type": "601011"}}}
    resp = await env.cr.handle_activate(_Req(character_id="chuxue-001"))
    assert resp.status == 200
    assert _payload(resp)["data"]["tts"]["voice_type"] == "601011"


@pytest.mark.asyncio
async def test_activate_tts_voice_type_falls_back_to_factory(env):
    """未调校 → tts.voice_type 回落 profile 出厂值（行为与调校前零差异）。"""
    resp = await env.cr.handle_activate(_Req(character_id="chuxue-001"))
    assert resp.status == 200
    assert _payload(resp)["data"]["tts"]["voice_type"] == "502001"


@pytest.mark.asyncio
async def test_activate_tts_voice_type_accepts_legacy_voice_key(env):
    """天使心旧数据用 voice 键存调校，同样生效（双键归一）。"""
    env.user_store.prefs["user-1"] = {"character_overrides": {
        "chuxue-001": {"voice": "601009"}}}
    resp = await env.cr.handle_activate(_Req(character_id="chuxue-001"))
    assert resp.status == 200
    assert _payload(resp)["data"]["tts"]["voice_type"] == "601009"


# ───────────────────────── /messages ─────────────────────────

@pytest.mark.asyncio
async def test_message_requires_content(env):
    resp = await env.cr.handle_message(_Req(character_id="chuxue-001"))
    assert resp.status == 400
    assert _payload(resp)["message"] == "content 必填"


@pytest.mark.asyncio
async def test_message_owned_not_held_rejected(env):
    """核心闸门：不能靠「直接发消息」绕进未持有的 owned 角色。"""
    resp = await env.cr.handle_message(
        _Req(body={"content": "你好"}, character_id="mojito-005"))
    assert resp.status == 404
    assert _payload(resp)["message"] == "unknown_character"


@pytest.mark.asyncio
async def test_message_happy_app_channel_and_memory_tier(llm_ok):
    env = llm_ok
    resp = await env.cr.handle_message(
        _Req(body={"content": "你好"}, character_id="chuxue-001"))
    assert resp.status == 200
    d = _payload(resp)["data"]
    assert d["reply"] == "回复:你好"
    assert d["channel"] == CHANNEL_APP
    assert d["device_should_speak"] is False     # app 面设备静音
    # 记忆分层：无卡=短期，回升级提示
    assert d["memory_tier"] == env.cm.TIER_SHORT
    assert d["memory_hint"] == env.cm.SHORT_TERM_HINT
    # 未激活时自动按 app 面补设（与 /activate 同语义）
    assert ("user-1", "chuxue-001") in env.router.app_opens
    # 历史落库（user + assistant 各一条）
    assert len(env.history["pushed"]) == 2


@pytest.mark.asyncio
async def test_message_voice_uses_user_tuning(llm_ok):
    """契约 §2.3：响应 voice 是前端「权威 voice」（直喂 /tts？voice=），
    用户调校音色必须在对话出口生效（旧实现只走 list/detail，此处断链）。"""
    env = llm_ok
    env.user_store.prefs["user-1"] = {"character_overrides": {
        "chuxue-001": {"voice_type": "601011"}}}
    resp = await env.cr.handle_message(
        _Req(body={"content": "你好"}, character_id="chuxue-001"))
    assert resp.status == 200
    assert _payload(resp)["data"]["voice"] == "601011"


@pytest.mark.asyncio
async def test_message_voice_falls_back_to_factory(llm_ok):
    """未调校 → voice 回落 route_response 出厂值（与调校前零差异）。"""
    env = llm_ok
    resp = await env.cr.handle_message(
        _Req(body={"content": "你好"}, character_id="chuxue-001"))
    assert resp.status == 200
    # fake router 的 route_response 出厂 voice 字面量
    assert _payload(resp)["data"]["voice"] == "voice-test"


@pytest.mark.asyncio
async def test_message_llm_unavailable_503(monkeypatch, env):
    monkeypatch.setattr(env.cr, "_get_llm", lambda: None)
    resp = await env.cr.handle_message(
        _Req(body={"content": "你好"}, character_id="chuxue-001"))
    assert resp.status == 503


@pytest.mark.asyncio
async def test_message_no_system_prompt_422(env):
    env.router.system_prompt = None
    resp = await env.cr.handle_message(
        _Req(body={"content": "你好"}, character_id="chuxue-001"))
    assert resp.status == 422


# ───────────────────────── /session ─────────────────────────

@pytest.mark.asyncio
async def test_session_empty_returns_nulls(env):
    resp = await env.cr.handle_get_session(_Req())
    assert _payload(resp)["data"] == {
        "character_id": None, "channel": None, "device_id": None}


@pytest.mark.asyncio
async def test_session_returns_active(env):
    env.router.sessions.set_active("user-1", "yaoye-004",
                                   channel=CHANNEL_APP, device_id="box-1")
    resp = await env.cr.handle_get_session(_Req())
    d = _payload(resp)["data"]
    assert d["character_id"] == "yaoye-004"
    assert d["channel"] == CHANNEL_APP
    assert d["device_id"] == "box-1"


@pytest.mark.asyncio
async def test_clear_session_clears_session_and_history(env):
    env.router.sessions.set_active("user-1", "chuxue-001", channel=CHANNEL_APP)
    resp = await env.cr.handle_clear_session(_Req())
    assert resp.status == 200
    assert "user-1" in env.router.sessions.cleared
    # 清会话同时清该角色历史（兜底时 cid=None 清全部）
    assert ("user-1", "chuxue-001") in env.history["cleared"]
