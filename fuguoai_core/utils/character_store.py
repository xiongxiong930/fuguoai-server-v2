"""NFC 身份卡角色存储 + 双交互面会话路由（NFC 产品线后端骨架）。

设计来源：NFC 标签只存 character_id；人格全在服务端（防 prompt injection、可运营）。
同一角色有两个对话面：device（设备，碰卡说话、设备发声）与 app（小程序，后端直接对
小程序交流、设备静音）。后端对角色出结果后按 active_channel 路由，仅激活面发声。

关键不变量：
- 角色绑定按 uid（非 device_id）——小程序与设备共享同一角色，必须走账号域。
- active_channel 最后交互面获胜（latest wins）；同卡重复碰 = 幂等 no-op。
- 设备静音是结构性的：channel==app 时 router 根本不产出 device TTS 指令。

本文件只负责「存储 + 路由决策 + 人格注入」，不含真实推送。真实推送（TTS→设备
downlink_play / 文本→小程序 events WS）由调用方按 live 连接注入，见 route_response
返回的 DeliveryTarget。这样骨架可独立单测，不牵动现有 ASR/LLM/TTS 链路。

持久化：sessions 落 data/character_sessions.json（与 NetworkedReminderStore 同范式）。
角色目录默认 data/characters.yaml（admin 维护，绝不接受客户端写）。
"""

import json
import os
import threading
from dataclasses import dataclass, field
from typing import Optional

from fuguoai_config.logger import setup_logging

TAG = __name__
logger = setup_logging()

_DATA_DIR = os.environ.get("FUGUOAI_DATA_DIR", "/opt/fuguoai-server/data")
_PROFILES_PATH = os.path.join(_DATA_DIR, "characters.yaml")
_SESSIONS_PATH = os.path.join(_DATA_DIR, "character_sessions.json")

CHANNEL_DEVICE = "device"
CHANNEL_APP = "app"
_VALID_CHANNELS = (CHANNEL_DEVICE, CHANNEL_APP)

# 性别→默认声线（角色未显式配 voice 时回退；具体声线 id 由 TTS 层最终解析）
DEFAULT_VOICE_BY_GENDER = {
    "female": "zh-CN-XiaoxiaoNeural",
    "male": "zh-CN-YunxiNeural",
    "neutral": "zh-CN-YunyangNeural",
}

# 角色分层（ownership tiering，2026-10-09 设计稿 P1）：
# - preset：预设基础角色，所有人可用（含无卡试玩用户）
# - owned ：需持有对应实体卡才可用，其自定义人格层由持有者写
# 判定「是否可用」= tier==preset or card_registry.owns(uid, character_id)
TIER_PRESET = "preset"
TIER_OWNED = "owned"
_VALID_TIERS = (TIER_PRESET, TIER_OWNED)

# 附录 A 共用兜底过渡自语（DEC-046）：角色未配 transition_line 时下发此句
DEFAULT_TRANSITION_LINE = "……好像睡了很久。"


def _normalize_tier(v):
    """未知/缺失/非法 tier 一律按 preset（保守：宁可多给，不可误锁）。"""
    t = (v or "").strip().lower()
    return t if t in _VALID_TIERS else TIER_PRESET


# ---------------------------------------------------------------------------
# 角色目录
# ---------------------------------------------------------------------------
class CharacterProfile:
    def __init__(self, raw: dict):
        self.character_id = raw["character_id"]
        self.name = raw.get("name", self.character_id)
        self.gender = raw.get("gender", "neutral")
        self.age = raw.get("age")
        self.personality = raw.get("personality") or []
        self.system_prompt = raw.get("system_prompt", "")
        self.voice = raw.get("voice") or DEFAULT_VOICE_BY_GENDER.get(self.gender)
        self.avatar = raw.get("avatar")
        self.tags = raw.get("tags") or []
        self.tier = _normalize_tier(raw.get("tier"))
        # 产品隔离维度（2026-10-16 新增）：空串=无产品归属（向后兼容，
        # 仅当调用方显式传 product 时才按此过滤；不传则全量返回）。
        self.product = (raw.get("product") or "").strip()
        # 展示型字段（不影响 LLM；LLM 只看 system_prompt）：背景故事/唤醒台词/灯效/音效
        self.backstory = raw.get("backstory") or ""
        self.awaken_quote = raw.get("awaken_quote") or ""
        self.led_config = raw.get("led_config") or None
        self.awaken_sound = raw.get("awaken_sound") or ""
        # 契约字段映射（2026-10-16 线 0，后端对接文档 §4.1）：技术契约用
        # voice_type/background，159 现盘 yaml 用 voice/backstory（名不同值一致）。
        # 读取层双名兼容 + to_dict 输出别名，**不改 yaml 旧字段**（天使心 5 角色
        # 既有数据与已部署调用方不破）。契约名优先，缺则回回落盘旧名。
        self.voice_type = raw.get("voice_type") or self.voice
        self.background = raw.get("background") or self.backstory
        # 附录 A 三字段（DEC-046/048，线 0 数据补齐）：speed 出厂语速偏移、
        # transition_line 首激过渡自语（缺省=共用兜底句）、factory_sliders 滑杆出厂值。
        # 天使心旧角色未配 → speed/sliders 为 None、transition_line 落兜底，不炸。
        # ⚠️ speed→腾讯 TTS Speed（整数 -2~+6）的换算无官方依据，属联调项，
        #    此处只做字段落地与下发，不在读取层猜映射。
        self.speed = raw.get("speed")
        self.transition_line = raw.get("transition_line") or DEFAULT_TRANSITION_LINE
        self.factory_sliders = raw.get("factory_sliders") or None

    def to_dict(self):
        return {
            "character_id": self.character_id,
            "name": self.name,
            "gender": self.gender,
            "age": self.age,
            "personality": self.personality,
            "system_prompt": self.system_prompt,
            "voice": self.voice,
            "avatar": self.avatar,
            "tags": self.tags,
            "tier": self.tier,
            "product": self.product,
            "backstory": self.backstory,
            "awaken_quote": self.awaken_quote,
            "led_config": self.led_config,
            "awaken_sound": self.awaken_sound,
            # 契约别名字段（线 0）：与上面 voice/backstory 同值并存，
            # 前端按契约字段渲染，旧调用方继续读 voice/backstory。
            "voice_type": self.voice_type,
            "background": self.background,
            "speed": self.speed,
            "transition_line": self.transition_line,
            "factory_sliders": self.factory_sliders,
        }


class CharacterProfileStore:
    """角色目录：从 data/characters.yaml 加载；支持 admin 运行时增删（不走客户端）。"""

    def __init__(self, profiles_path=None):
        self._path = profiles_path or _PROFILES_PATH
        self._lock = threading.Lock()
        self._cache = None

    def _load(self):
        if self._cache is not None:
            return self._cache
        try:
            import yaml
            with open(self._path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except FileNotFoundError:
            raw = {}
        except Exception as e:  # 解析失败不应让服务崩
            logger.bind(tag=TAG).error(f"加载角色目录失败: {e}")
            raw = {}
        profiles = {}
        for cid, item in (raw.get("characters", raw) if isinstance(raw, dict) else {}).items():
            if not isinstance(item, dict):
                continue
            item = dict(item)
            item["character_id"] = cid
            try:
                profiles[cid] = CharacterProfile(item)
            except KeyError:
                logger.bind(tag=TAG).warning(f"角色 {cid} 缺 character_id/name/system_prompt，跳过")
        self._cache = profiles
        return profiles

    def reload(self):
        """admin 改了 yaml 后调用，清空缓存重新加载。"""
        with self._lock:
            self._cache = None
        return self._load()

    def get(self, character_id: str) -> Optional[CharacterProfile]:
        return self._load().get(character_id)

    def list_all(self, product=None) -> list:
        profiles = list(self._load().values())
        if product:
            product = str(product).strip()
            if product:
                # 仅当调用方显式指定 product 时才过滤；空 product（未打标）的角色
                # 不在任何产品视图内（避免旧 5 角色污染活字匣），但显式不传则全量。
                profiles = [p for p in profiles if p.product == product]
        return profiles

    def validate_id(self, character_id: str) -> bool:
        """NFC 上报的 id 是否在白名单内（未知 id 一律拒，不落库、不注入）。"""
        return character_id in self._load()


# ---------------------------------------------------------------------------
# 会话（uid 域，含 active_channel 与当前激活角色）
# ---------------------------------------------------------------------------
@dataclass
class CharacterSession:
    uid: str
    character_id: Optional[str] = None
    channel: str = CHANNEL_DEVICE
    device_id: Optional[str] = None
    last_activity_at: float = 0.0

    def to_dict(self):
        return {
            "uid": self.uid,
            "character_id": self.character_id,
            "channel": self.channel,
            "device_id": self.device_id,
            "last_activity_at": self.last_activity_at,
        }

    @classmethod
    def from_dict(cls, d: dict):
        return cls(
            uid=d["uid"],
            character_id=d.get("character_id"),
            channel=d.get("channel", CHANNEL_DEVICE),
            device_id=d.get("device_id"),
            last_activity_at=d.get("last_activity_at", 0.0),
        )


class CharacterSessionStore:
    """uid → 当前激活角色 + active_channel。落 data/character_sessions.json。"""

    def __init__(self, sessions_path=None):
        self._path = sessions_path or _SESSIONS_PATH
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self, db: dict):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    def get(self, uid: str) -> Optional[CharacterSession]:
        db = self._load()
        d = db.get(uid)
        return CharacterSession.from_dict(d) if d else None

    def set_active(self, uid: str, character_id: str, channel: Optional[str] = None,
                   device_id: Optional[str] = None, now: float = 0.0) -> CharacterSession:
        """设置/切换激活角色。channel 省略则保持当前面（默认 device）。"""
        if channel is not None and channel not in _VALID_CHANNELS:
            raise ValueError(f"channel 必须是 {_VALID_CHANNELS}")
        with self._lock:
            db = self._load()
            sess = CharacterSession.from_dict(db.get(uid, {"uid": uid})) if db.get(uid) else CharacterSession(uid=uid)
            # 同角色同面 = 幂等 no-op（不打扰、不重复写）
            if sess.character_id == character_id and (channel is None or sess.channel == channel):
                return sess
            sess.character_id = character_id
            if channel is not None:
                sess.channel = channel
            if device_id is not None:
                sess.device_id = device_id
            sess.last_activity_at = now or sess.last_activity_at
            db[uid] = sess.to_dict()
            self._save(db)
        return sess

    def set_channel(self, uid: str, channel: str, now: float = 0.0) -> Optional[CharacterSession]:
        """切换交互面（最后交互面获胜）。保留已激活角色。"""
        if channel not in _VALID_CHANNELS:
            raise ValueError(f"channel 必须是 {_VALID_CHANNELS}")
        with self._lock:
            db = self._load()
            sess = db.get(uid)
            if not sess:
                return None
            sess = CharacterSession.from_dict(sess)
            if sess.channel == channel:
                return sess
            sess.channel = channel
            sess.last_activity_at = now or sess.last_activity_at
            db[uid] = sess.to_dict()
            self._save(db)
        return sess

    def clear(self, uid: str) -> bool:
        """回到基础人格（无激活角色）。"""
        with self._lock:
            db = self._load()
            if uid not in db:
                return False
            del db[uid]
            self._save(db)
        return True


# ---------------------------------------------------------------------------
# 路由决策 + 人格注入（骨架核心）
# ---------------------------------------------------------------------------
@dataclass
class DeliveryTarget:
    """route_response 的产物。调用方据此把文本送到正确的一面。"""
    target: str            # "device" | "app" | "none"
    character_id: Optional[str] = None
    name: Optional[str] = None
    text: str = ""
    voice: Optional[str] = None

    def device_should_speak(self) -> bool:
        """设备是否应发声：仅 device 面且确有角色时。app 面永远 False。"""
        return self.target == CHANNEL_DEVICE


class CharacterRouter:
    """组合 profile + session，产出路由决策与人格 system_prompt。"""

    def __init__(self, profiles: CharacterProfileStore, sessions: CharacterSessionStore):
        self.profiles = profiles
        self.sessions = sessions

    def on_nfc_tap(self, uid: str, character_id: str, device_id: Optional[str] = None,
                   source: str = CHANNEL_DEVICE, now: float = 0.0) -> dict:
        """NFC 碰卡入口。source 标识读卡端：

        - source=device（融合盒 / 音箱碰卡）→ device 面，设备发声（LLM+TTS）。
        - source=app（手机小程序 NFC 碰卡）→ app 面，设备结构性静音（只 LLM 文本）。

        device_id 只在 source==device 时落库（手机碰卡不绑定 TTS 设备）。
        返回 ack 摘要，含 channel 与 device_should_speak 供调用方路由。
        """
        if source not in _VALID_CHANNELS:
            return {"ok": False, "reason": "bad_source"}
        if not self.profiles.validate_id(character_id):
            return {"ok": False, "reason": "unknown_character"}
        channel = source  # 读卡来源直接映射对话面
        did = device_id if channel == CHANNEL_DEVICE else None
        self.sessions.set_active(uid, character_id, channel=channel,
                                 device_id=did, now=now)
        p = self.profiles.get(character_id)
        return {"ok": True, "character_id": character_id, "name": p.name,
                "gender": p.gender, "channel": channel,
                "device_should_speak": channel == CHANNEL_DEVICE,
                "summary": f"已切换为{p.name}"}

    def on_app_open(self, uid: str, character_id: str, now: float = 0.0) -> dict:
        """小程序选角色并进入对话：设激活角色 + 切到 app 面（设备静音）。"""
        if not self.profiles.validate_id(character_id):
            return {"ok": False, "reason": "unknown_character"}
        self.sessions.set_active(uid, character_id, channel=CHANNEL_APP, now=now)
        p = self.profiles.get(character_id)
        return {"ok": True, "character_id": character_id, "name": p.name,
                "channel": CHANNEL_APP, "device_should_speak": False}

    def switch_channel(self, uid: str, channel: str, now: float = 0.0) -> Optional[CharacterSession]:
        return self.sessions.set_channel(uid, channel, now=now)

    def route_response(self, uid: str, text: str) -> DeliveryTarget:
        """LLM 出结果后调用：按 active_channel 决定发哪一面。

        设备静音 = channel==app 时 target 直接是 "app"，不产出 device 指令。
        """
        sess = self.sessions.get(uid)
        if not sess or not sess.character_id:
            return DeliveryTarget(target="none")
        p = self.profiles.get(sess.character_id)
        if not p:
            return DeliveryTarget(target="none")
        if sess.channel == CHANNEL_APP:
            # 后端直接对小程序交流，设备不发声
            return DeliveryTarget(target=CHANNEL_APP, character_id=p.character_id,
                                 name=p.name, text=text, voice=p.voice)
        return DeliveryTarget(target=CHANNEL_DEVICE, character_id=p.character_id,
                             name=p.name, text=text, voice=p.voice)

    def build_system_prompt(self, uid: str) -> Optional[str]:
        """LLM 拼系统提示时调用：返回激活角色的服务端 system_prompt，否则 None。"""
        sess = self.sessions.get(uid)
        if not sess or not sess.character_id:
            return None
        p = self.profiles.get(sess.character_id)
        return p.system_prompt if p else None

    def resolve_voice(self, uid: str) -> Optional[str]:
        """TTS 选声线时调用：角色 voice → 回退性别默认声线。"""
        sess = self.sessions.get(uid)
        if not sess or not sess.character_id:
            return None
        p = self.profiles.get(sess.character_id)
        return p.voice if p else None


# ---------------------------------------------------------------------------
# 单例（与 NetworkedReminderStore 同范式）
# ---------------------------------------------------------------------------
_profile_store = None
_session_store = None
_router = None


def get_character_profile_store() -> CharacterProfileStore:
    global _profile_store
    if _profile_store is None:
        _profile_store = CharacterProfileStore()
    return _profile_store


def get_character_session_store() -> CharacterSessionStore:
    global _session_store
    if _session_store is None:
        _session_store = CharacterSessionStore()
    return _session_store


def get_character_router() -> CharacterRouter:
    global _router
    if _router is None:
        _router = CharacterRouter(get_character_profile_store(),
                                 get_character_session_store())
    return _router
