"""天使心 NFC 角色产品线 REST（新增路由，HTTP 只扩不删改）。

前缀 /api/v1，统一走 core.guards.api_guard（小程序 JWT）。
接口（与《天使心后端接口文档 v0.1》§4/§5/§6 对齐）：
- GET    /api/v1/characters                          角色目录（写卡页取 id 用）
- GET    /api/v1/characters/{character_id}           角色详情（未知→404）
- POST   /api/v1/nfc/tap                             设备/手机碰卡 → 设激活角色+对话面
         body: {card_uid, device_id?, source?}       source∈{device,app}，默认 device
                                                    （模型 b：只认后端 card_registry）
- POST   /api/v1/nfc/claim                           领卡（一次性把 owner_uid 绑到当前 uid）
         body: {card_uid}                            已领 409 / 未知卡 404 unknown_card
- GET    /api/v1/cards/me                            我的卡（编号/角色/领卡/收藏/代数）
- GET    /api/v1/characters/{character_id}/lineage   该角色卡的履历（历任脱敏）
- POST   /api/v1/cards/{serial}/wipe                 一键清空（记忆+历史+自定义人格）
- POST   /api/v1/cards/{serial}/transfer             二手转手（须先 wipe，否则 409）
- PUT    /api/v1/cards/{serial}/collect              收藏 / 取消收藏  body: {collected?}
- POST   /api/v1/characters/{character_id}/activate  小程序点开角色 → app 面（设备静音）
         body: {source?}                             默认 app
- POST   /api/v1/characters/{character_id}/messages  对话（app 面回文本 / device 面推 TTS）
         body: {content, stream?}
- GET    /api/v1/session                             当前会话（character_id/channel/device_id）
- POST   /api/v1/session/clear                       清激活角色，回到默认态

角色分层（ownership tiering，2026-10-09 设计稿 P1）
--------------------------------------------------
- 角色带 tier：preset（所有人可用，无卡试玩）/ owned（需持有实体卡）。
- 可见性 = tier==preset or card_registry.owns(uid, character_id)；owned 角色对
  未持卡 uid 一律 404（人物设定一同不泄露）。闸门在后端，前端绕不过。
- /nfc/tap 由 character_id 改收 card_uid：卡只带只读 uid，映射由后端注册裁决，
  客户端自报 character_id 一概不认（防克隆）。未知卡 → 404 unknown_card（前端留登录页）。
- owner_uid 仅在 /nfc/claim 绑定一次；日常碰卡不改 owner（捡到已领的卡也占不走）。
- 记忆 key 用微信 uid 而非 card_uid → 丢卡不丢数据（设计稿 §八）。
- 记忆分层（P2）：持卡=长期（MemoryStore 抽取+检索注入 <memory>）；无卡=仅短期
  滚动窗口，不落长期、不检索，并回 memory_hint 提示升级——刻意"不够用"。

设计要点
--------
- 角色人格全在服务端 character_store（data/characters.yaml），前端不持有 system_prompt。
- 对话面由 session.active_channel 决定：device 面 LLM→TTS 推设备；app 面只回文本。
- LLM 实例懒加载（与 modules_initialize 同配置源），失败降级 503，不拖垮进程。
- 设备 TTS 复用 networked_reminder 已验证的 speak_text_to_device（双路探测新/老固件）。
- 对话历史为演示级滚动窗口（每 uid+character 最近 6 轮），落盘持久化（data/character_history.json，
  重启不丢），多心互不串味；持久记忆后续接 memory/family_knowledge。

注：device 面语音对话（设备端 ASR→LLM→TTS 的完整音频通道）走既有 connection.py
对话管线；本模块负责「设备碰卡→注入角色 system_prompt + 设备发声路由」。
- 设备碰卡（source=device，由客户小程序读卡后调 POST /api/v1/nfc/tap）后，本模块按
  device_id 取活连接并调 connection.py:1625 change_system_prompt(角色 system_prompt)
  （该 prompt 仅连接建立时初始化一次、无每轮重置，故注入后跨轮保持），设备后续语音对话
  即以此角色人格进行——这是「device 面端到端」的核心。失败 fail-soft（设备离线只落会话）。
- device 面 TTS 音色 v1 用设备默认；角色 voice 字段暂只回传小程序侧（待定是否按角色覆盖）。
- messages 端点 channel=device 时另可推 TTS 到 device_id，与设备既有音频管线并存。
"""

import asyncio
import json
import logging
import os
import queue
import threading

from aiohttp import web

from fuguoai_core.api.registry import register_routes
from fuguoai_core.guards import api_guard
from fuguoai_core.utils.character_store import (
    get_character_router,
    CHANNEL_DEVICE,
    CHANNEL_APP,
    TIER_PRESET,
    TIER_OWNED,
)
from fuguoai_core.utils.card_registry import get_card_registry
from fuguoai_core.utils.angel_heart_store import get_user_store
from fuguoai_core.utils import character_memory

TAG = "character_routes"

logger = logging.getLogger(TAG)

# ── 天使心 NFC 设备模型（微信 mp 后台登记）──────────────────────────────
# 设备型号名：FG-NFC-ROLE-01；model_id：wAXz2K4hDQQFddN8AOohfA
# 该 model_id 是 mp 后台「NFC 标签调起小程序」能力登记的设备模型标识，
# 真正生成 NDEF openlink（weixin://dl/business/?t=xxx，卡面写卡用）发生在
# mp 后台 / 固件写卡工具侧，本常量仅作为后端单一出处与审计留痕，不在运行时校验。
NFC_ROLE_DEVICE_MODEL = "FG-NFC-ROLE-01"
NFC_ROLE_MODEL_ID = "wAXz2K4hDQQFddN8AOohfA"


# ---------------------------------------------------------------------------
# 统一响应封装（与文档 §2 一致：{code,message,data}）
# ---------------------------------------------------------------------------
def _dumps(o):
    return json.dumps(o, ensure_ascii=False)


def _sse(obj):
    """SSE 事件帧：data: <json>\n\n（UTF-8）。流式对话逐块吐文本用。"""
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")


# ---------------------------------------------------------------------------
# LLM 同步流 → 异步桥（单事件循环架构，同步网络 IO 绝不进协程）
# ---------------------------------------------------------------------------
_LLM_STREAM_DONE = object()


async def _llm_pieces_async(llm, dialogue, stop_evt=None):
    """把同步 LLM 流生成器下沉到后台线程，协程侧异步逐块消费。

    本服务为单事件循环架构：openai 同步流每等一个 chunk 都是阻塞 socket 读，
    在协程里直接 for 迭代会卡死整个事件循环（同进程 WS 心跳/其他 HTTP 全挂起）。
    这里用 queue 桥接：producer 线程迭代生成器，消费侧 run_in_executor 取块，
    既保流式实时性，又绝不阻塞循环。
    stop_evt：调用方（如 SSE 客户端断开）置位后，producer 尽快停止迭代。
    """
    q = queue.Queue()

    def _produce():
        try:
            for piece in llm.response("", dialogue):
                if stop_evt is not None and stop_evt.is_set():
                    break
                if piece:
                    q.put(piece)
        except Exception as e:  # 异常经队列原样传给消费侧抛出
            q.put(e)
        finally:
            q.put(_LLM_STREAM_DONE)

    threading.Thread(target=_produce, daemon=True, name="llm-stream-bridge").start()
    loop = asyncio.get_running_loop()
    try:
        while True:
            item = await loop.run_in_executor(None, q.get)
            if item is _LLM_STREAM_DONE:
                return
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        if stop_evt is not None:
            stop_evt.set()


def _collect_no_stream(llm, dialogue):
    """非流式聚合：与 base.response_no_stream 同语义（provider 异常降级为部分文本）。

    在 to_thread 里跑，不占事件循环。带完整 dialogue（含历史），
    response_no_stream 自建 [system, user] 不带历史，故此处自聚合。
    """
    parts = []
    try:
        for piece in llm.response("", dialogue):
            if piece:
                parts.append(piece)
    except Exception as e:
        logger.warning(f"[character] 非流式生成异常（降级为部分文本）: {e}")
    return "".join(parts)


def _build_dialogue(system_prompt, history, content):
    """system + 滚动历史 + 本轮 user。历史封顶 _HISTORY_LIMIT 轮，只认 user/assistant。"""
    dialogue = [{"role": "system", "content": system_prompt}]
    for h in history:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            dialogue.append({"role": h["role"], "content": h["content"]})
    dialogue.append({"role": "user", "content": content})
    return dialogue


def _add_cors(request, resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "content-type, authorization"
    return resp


def _ok(data):
    return web.json_response({"code": 0, "message": "", "data": data}, dumps=_dumps)


def _err(status, message):
    return web.json_response({"code": status, "message": message}, status=status, dumps=_dumps)


def _user_id(user):
    return (user or {}).get("user_id") or (user or {}).get("openid")


def _apply_user_overrides(uid, d):
    """把该 uid 在 users.json 里的 character_overrides 合并进角色字典。

    只覆盖 name/personality/voice/voice_type/led_config/awaken_sound；
    system_prompt、gender、age 等归 admin 维护的字段客户端写不进来，
    保持后端契约。
    """
    if not uid or not isinstance(d, dict):
        return d
    try:
        prefs = get_user_store().get_preferences(uid)
        ov = (prefs or {}).get("character_overrides", {}).get(d.get("character_id"), {})
        if ov:
            for k in ("name", "personality", "voice", "led_config", "awaken_sound"):
                if k in ov:
                    d[k] = ov[k]
            # 调校音色双键归一（契约 §2.3）：前端 PG-TUNE 存 voice_type
            # （腾讯 VoiceType 数字 ID），天使心旧数据用 voice；本部署二者
            # 同值（yaml voice 即数字 ID）。任一键出现都同步写 d 的
            # voice/voice_type，前端读哪个键都拿到调校值。
            tuned = ov.get("voice_type") or ov.get("voice")
            if tuned:
                d["voice"] = tuned
                d["voice_type"] = tuned
    except Exception:
        pass
    return d


def _effective_voice(uid, cid, fallback=None):
    """用户调校音色优先，回落出厂值（契约 §2.3「音色取用户 binding 的
    tuning.voice_type（用户可能改过），回落 profile.voice_type」）。

    与 _apply_user_overrides 同数据源（users.json character_overrides），
    但用于**非字典出口**：activate 的 tts.voice_type 与对话响应的 voice
    （前端把它当「权威 voice」直接喂 /tts？voice=，故调校必须在这些出口生效）。
    读不到覆盖（未调校/异常）时原样回落，行为与调校前零差异。
    """
    try:
        prefs = get_user_store().get_preferences(uid)
        ov = (prefs or {}).get("character_overrides", {}).get(cid) or {}
        v = ov.get("voice_type") or ov.get("voice")
        v = str(v).strip() if v else ""
        return v or fallback
    except Exception:
        return fallback


# ---------------------------------------------------------------------------
# 对话历史（每 uid+character 滚动窗口，落盘持久化——重启不丢，且多心互不串味）
# ---------------------------------------------------------------------------
_DATA_DIR = os.environ.get("FUGUOAI_DATA_DIR", "/opt/fuguoai-server/data")
_HISTORY_PATH = os.path.join(_DATA_DIR, "character_history.json")
_HISTORY_LOCK = threading.Lock()
_HISTORY_LIMIT = 6


def _load_history_file():
    """uid -> character_id -> [turn]。失败按空库处理（绝不拖垮启动）。"""
    try:
        with open(_HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception as e:
        logger.warning(f"[character] 读对话历史失败，按空处理: {e}")
        return {}


def _save_history_file(db):
    try:
        os.makedirs(os.path.dirname(_HISTORY_PATH), exist_ok=True)
        tmp = _HISTORY_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _HISTORY_PATH)
    except Exception as e:
        logger.warning(f"[character] 写对话历史失败(跳过持久化): {e}")


# 启动时从磁盘载入到内存（进程重启后历史仍在）；内存为准、写时回写磁盘。
# 该路径在 data/ bind mount 内，即使容器按镜像重建也不丢。
_HISTORY = _load_history_file()


def _push_history(uid, character_id, role, content):
    with _HISTORY_LOCK:
        hist = _HISTORY.setdefault(uid, {}).setdefault(character_id, [])
        hist.append({"role": role, "content": content})
        if len(hist) > _HISTORY_LIMIT * 2:
            del hist[: len(hist) - _HISTORY_LIMIT * 2]
        _save_history_file(_HISTORY)


def _get_history(uid, character_id):
    with _HISTORY_LOCK:
        return list(_HISTORY.get(uid, {}).get(character_id, []))


def _clear_history(uid, character_id=None):
    """清某心历史（给定 character_id）或某 uid 全部（character_id=None，兜底）。"""
    with _HISTORY_LOCK:
        if character_id is None:
            _HISTORY.pop(uid, None)
        else:
            _HISTORY.get(uid, {}).pop(character_id, None)
        _save_history_file(_HISTORY)


# ---------------------------------------------------------------------------
# LLM 懒加载（与 modules_initialize.initialize_modules 同源配置）
# ---------------------------------------------------------------------------
_LLM = None
_LLM_LOCK = threading.Lock()


def _get_llm():
    global _LLM
    if _LLM is not None:
        return _LLM
    with _LLM_LOCK:
        if _LLM is not None:
            return _LLM
        try:
            from fuguoai_core.utils.cache.manager import cache_manager, CacheType
            from fuguoai_core.utils.llm import create_instance
            cfg = cache_manager.get(CacheType.CONFIG, "main_config")
            if not cfg:
                logger.warning("[character] 无 main_config，LLM 不可用")
                return None
            sel = cfg["selected_module"]["LLM"]
            llm_type = sel if "type" not in cfg["LLM"][sel] else cfg["LLM"][sel]["type"]
            _LLM = create_instance(llm_type, cfg["LLM"][sel])
            logger.info(f"[character] LLM 实例就绪({llm_type})")
        except Exception as e:
            logger.warning(f"[character] LLM 懒加载失败(后续重试): {e}")
            return None
    return _LLM


# ---------------------------------------------------------------------------
# 鉴权辅助
# ---------------------------------------------------------------------------
async def _auth(request):
    """返回 (uid, err_resp)。err_resp 非空则调用方直接 return。"""
    user = api_guard(request)
    if isinstance(user, web.Response):
        return None, _add_cors(request, user)
    uid = _user_id(user)
    if not uid:
        return None, _add_cors(request, _err(401, "未登录或用户标识缺失"))
    return uid, None


async def _body(request):
    try:
        return await request.json()
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 角色分层闸门（owned 仅持有者可见/可用；preset 人人可用）
# ---------------------------------------------------------------------------
def _is_owned_tier(p) -> bool:
    return getattr(p, "tier", TIER_PRESET) == TIER_OWNED


def _binding_admin_uids() -> set:
    """character-binding 运营白名单（config: character_binding.admin_uids）。

    缺省空 = 最严：owned-tier 角色绑定对所有人关闭（防 mock UID 免费解锁
    锁定角色）。config_holder 支持零重启热更新，故 per-call 读取不缓存。
    名单只放 data/.config.yaml（gitignored），绝不进 git 跟踪的 config.yaml。
    """
    try:
        from fuguoai_core.utils.config_holder import get_config
        cfg = (get_config() or {}).get("character_binding") or {}
        raw = cfg.get("admin_uids") or []
        if isinstance(raw, str):
            raw = [raw]
        return {str(u).strip() for u in raw if str(u).strip()}
    except Exception:
        return set()


def _is_binding_admin(uid) -> bool:
    return bool(uid) and uid in _binding_admin_uids()


def _visible_to(uid, p, registry) -> bool:
    """owned 角色需持有对应实体卡；preset 一律可见。

    闸门在后端：owned 角色对未持卡 uid 完全不出现在目录里，且详情/激活也 404，
    客户端无法通过改请求绕过。
    """
    if p is None:
        return False
    if not _is_owned_tier(p):
        return True
    return registry.owns(uid, p.character_id)


# 契约 §2.1「locked 只返回 name+visual+unlock_copy」的落地白名单（默认拒）：
# owned 未持卡角色在目录里**可见但锁定**，只下发身份/展示/分层标志，
# 绝不下发人格与调校字段——voice_type/speed/background/personality/
# system_prompt/transition_line/factory_sliders/awaken_quote/led_config/
# awaken_sound/backstory/voice/tags/age/product 全部禁。
# 分层标志（tier/owned/locked）保留：前端 is_locked 判定依赖它们。
# ⚠️ background 与 backstory 在 CharacterProfile 里同值（契约别名），
#    禁一个必须同时禁另一个，否则换键同值泄露。
_LOCKED_PUBLIC_FIELDS = ("character_id", "name", "avatar", "gender", "tier")


# ---------------------------------------------------------------------------
# 路由处理
# ---------------------------------------------------------------------------
async def handle_list_characters(request):
    """角色目录（按持卡态分层）：无卡 → preset + 「锁定」owned；持卡 → preset + 自有 owned。

    2026-10-10 改：owned 且未持卡的角色**不再从目录里消失**，而是返回并标记
    `locked: true`（名字/简介保留做「待解锁」展示，`system_prompt` 脱敏不下发）。
    真正的可用性闸门仍在 `_visible_to`：`/activate`、发消息、详情一律拦，
    所以展示归展示，用还是用不了。
    """
    uid, err = await _auth(request)
    if err:
        return err
    router = get_character_router()
    registry = get_card_registry()
    # 产品隔离（2026-10-16）：
    # - 活字匣前端传 ?product=huozixia → 仅返回标了该 product 的角色。
    # - 天使心小程序（angel_heart）旧调用**不传 product**，后端默认归到
    #   angel_heart，避免把新 4 角色（huozixia）漏进天使心视图。
    # - 旧 5 角色须标 product: angel_heart；新 4 角色标 product: huozixia。
    product = request.query.get("product") or "angel_heart"
    items = []
    for p in router.profiles.list_all(product=product):
        owns = registry.owns(uid, p.character_id)
        if _is_owned_tier(p) and not owns:
            # 可见但锁定（契约 §2.1）：**白名单构造**——只返回展示字段与分层
            # 标志，人格/调校字段一律不下发（防泄露 + 省流量）。
            # 2026-10-16 生产 E2E 实证：旧实现只 pop 了 system_prompt，
            # voice_type/speed/background/personality 四字段仍泄露 → 改默认拒。
            d = p.to_dict()
            d = {k: v for k, v in d.items() if k in _LOCKED_PUBLIC_FIELDS}
            d["owned"] = False
            d["locked"] = True
            items.append(d)
            continue
        if not _visible_to(uid, p, registry):
            continue  # 兜底：owned 且未持有（正常已被上面分支吃掉）
        d = p.to_dict()
        d["owned"] = owns
        d["locked"] = False
        _apply_user_overrides(uid, d)
        items.append(d)
    return _add_cors(request, _ok({"count": len(items), "characters": items}))


async def handle_get_character(request):
    uid, err = await _auth(request)
    if err:
        return err
    cid = request.match_info["character_id"].strip()
    router = get_character_router()
    p = router.profiles.get(cid)
    # owned 分层闸门：未持卡 uid 拿不到 owned 角色（连同 system_prompt 一起不泄露）
    if not _visible_to(uid, p, get_card_registry()):
        return _add_cors(request, _err(404, "未知角色"))
    d = p.to_dict()
    _apply_user_overrides(uid, d)
    return _add_cors(request, _ok(d))


def _inject_character_into_device(device_id, character_id):
    """device 面端到端：把角色 system_prompt 注入该 device_id 的活连接。

    取活连接走 intercom_registry._get_conn（与 speak_text_to_device 同模式，
    connection_registry._send/_online 的 factory）；设备不在线返回 None，fail-soft。
    change_system_prompt 在 connection.py:1625，仅连接建立时初始化一次、无每轮重置，
    故此处注入后跨轮保持，直接驱动设备后续 ASR→LLM→TTS 语音对话。

    跨线程：在 aiohttp handler 线程调 conn.change_system_prompt 改连接对象属性，
    与 networked_reminder_scheduler 跨线程 safe_send 同模式；全程 try 兜底，绝不抛给调用方。
    """
    if not device_id or not character_id:
        return False
    try:
        from fuguoai_core.utils.intercom_registry import _get_conn
    except Exception:
        return False
    try:
        conn = _get_conn(device_id)
    except Exception:
        return False
    if conn is None:
        logger.info(f"[character] 设备未在线,跳过 prompt 注入 did={device_id}")
        return False
    sp = get_character_router().profiles.get(character_id)
    if not sp or not sp.system_prompt:
        return False
    try:
        conn.change_system_prompt(sp.system_prompt)
        logger.info(
            f"[character] 已注入角色 system_prompt 到设备 did={device_id} cid={character_id}"
        )
        return True
    except Exception as e:
        logger.warning(f"[character] 设备 prompt 注入失败 did={device_id}: {e}")
        return False


async def _process_nfc_tap(uid, card_uid, device_id=None, source=CHANNEL_DEVICE):
    """碰卡激活核心逻辑（HTTP / 设备 WS 共用，无 I/O 框架依赖）。

    只认后端 card_registry，不认客户端自报 character_id（模型 b，防克隆）。
    返回 (http_status, payload)：成功 payload 为角色摘要 dict；失败 payload 为 reason 字符串。
    - 未知卡 → 404 unknown_card
    - 已知卡 → 激活其 character_id 给 uid；**不改 owner**（捡到已领的卡占不走）
    - device 面：把角色 system_prompt 注入该设备活连接（端到端，跨轮保持）
    """
    if not card_uid:
        return 400, "card_uid 必填"
    registry = get_card_registry()
    card = registry.get(card_uid)
    if not card:
        # 硬拒：非我方 NFC 在后端就被拦下，前端只是把 404 翻成提示
        return 404, "unknown_card"
    cid = (card.get("character_id") or "").strip()
    if not cid:
        return 404, "unknown_card"
    router = get_character_router()
    ack = router.on_nfc_tap(uid, cid, device_id=device_id, source=source)
    if not ack.get("ok"):
        return 404, ack.get("reason", "unknown_character")
    # device 面：把角色 system_prompt 注入该设备当前活连接（端到端，跨轮保持）
    if source == CHANNEL_DEVICE and device_id:
        _inject_character_into_device(device_id, cid)
    return 200, {
        "character_id": ack["character_id"],
        "name": ack.get("name"),
        "channel": ack.get("channel"),
        "device_should_speak": ack.get("device_should_speak"),
        "owned": registry.owns(uid, cid),         # 持有者 → 长期记忆 + 可自定义
        "unclaimed": not card.get("owner_uid"),   # 未领 → 客户端应调 /nfc/claim
    }


async def handle_nfc_tap(request):
    """碰卡激活（日常，HTTP 入口）：只认后端 card_registry，不认客户端自报 character_id。

    - 未知卡 → 404 unknown_card（前端 toast「卡片未授权」并留登录页，绝不进）。
    - 已知卡 → 激活其 character_id 给当前 uid；**不改 owner**（捡到已领的卡占不走）。
    - 返回 owned（是否该卡持有者，决定长期记忆/自定义）与 unclaimed（未领→客户端去 claim）。
    """
    uid, err = await _auth(request)
    if err:
        return err
    body = await _body(request)
    card_uid = (body or {}).get("card_uid", "").strip()
    device_id = (body or {}).get("device_id")
    source = (body or {}).get("source", CHANNEL_DEVICE)
    status, payload = await _process_nfc_tap(uid, card_uid, device_id, source)
    if status != 200:
        return _add_cors(request, _err(status, payload))
    return _add_cors(request, _ok(payload))


async def _process_nfc_claim(uid, card_uid):
    """领卡核心逻辑（HTTP / 设备 WS 共用，无 I/O 框架依赖）。

    把未绑定实体卡的 owner_uid 绑定到当前 uid；已领 → 409（防陌生人捡已领的卡占为己有）；
    未知卡 → 404。二手转手：P3 的 transfer 先 wipe 记忆再清 owner_uid，下一任走本接口重绑。
    返回 (http_status, payload)：成功 payload 为 claim 摘要 dict；失败 payload 为 reason 字符串。
    """
    if not card_uid:
        return 400, "card_uid 必填"
    ack = get_card_registry().claim(card_uid, uid)
    if not ack.get("ok"):
        reason = ack.get("reason")
        status = 404 if reason == "unknown_card" else 409
        return status, reason
    return 200, ack


async def handle_nfc_claim(request):
    """领卡（一次性，HTTP 入口）：把未绑定实体卡的 owner_uid 绑定到当前 uid。

    已领 → 409（防陌生人捡已领的卡占为己有）；未知卡 → 404。
    二手转手：P3 的 transfer 先 wipe 记忆再清 owner_uid，下一任走本接口重绑。
    """
    uid, err = await _auth(request)
    if err:
        return err
    body = await _body(request)
    card_uid = (body or {}).get("card_uid", "").strip()
    status, payload = await _process_nfc_claim(uid, card_uid)
    if status != 200:
        return _add_cors(request, _err(status, payload))
    return _add_cors(request, _ok(payload))


def _mask_uid(u):
    """履历里不暴露历任持有者的完整 uid（隐私）。"""
    s = str(u or "")
    if len(s) <= 5:
        return "***"
    return f"{s[:3]}***{s[-2:]}"


async def handle_cards_me(request):
    """我的卡：列出本 uid 持有的全部实体卡（编号/角色/领卡时间/收藏/代数）。

    **不返回 card_uid**——那是防克隆的关键，只能由 NFC 硬件读出后上报，
    绝不从 API 泄露；对外寻址一律用 serial（human 编号）。
    """
    uid, err = await _auth(request)
    if err:
        return err
    registry = get_card_registry()
    router = get_character_router()
    items = []
    for c in registry.list_by_uid(uid):
        cid = c.get("character_id")
        p = router.profiles.get(cid)
        items.append({
            "serial": c.get("serial"),
            "character_id": cid,
            "character_name": p.name if p else None,
            "claimed_at": c.get("claimed_at"),
            "created_at": c.get("created_at"),
            "collected": bool(c.get("collected")),
            "lineage_count": len(c.get("lineage") or []),
            "status": c.get("status"),
        })
    return _add_cors(request, _ok({"count": len(items), "cards": items}))


async def handle_character_lineage(request):
    """该角色的卡履历：编号 + 历任持有者（脱敏）+ 代数。仅持有者可看。"""
    uid, err = await _auth(request)
    if err:
        return err
    cid = request.match_info["character_id"].strip()
    registry = get_card_registry()
    mine = [c for c in registry.list_by_uid(uid) if c.get("character_id") == cid]
    if not mine:
        return _add_cors(request, _err(404, "unknown_card"))
    c = mine[0]
    lineage = c.get("lineage") or []
    return _add_cors(request, _ok({
        "serial": c.get("serial"),
        "character_id": cid,
        "generation": len(lineage),
        "lineage": [_mask_uid(u) for u in lineage],
    }))


async def handle_card_wipe(request):
    """前主人一键清空：删掉该角色在本 uid 下**全部**长期记忆+对话历史+自定义人格。

    二手转手的前置动作（隐私硬约束）。结构上聊天/记忆本就按微信 uid 存、下一任
    看不到；wipe 的意义是让前主人**彻底断开、不留残留**。
    """
    uid, err = await _auth(request)
    if err:
        return err
    serial = (request.match_info.get("serial") or "").strip()
    registry = get_card_registry()
    cuid, card = registry.get_by_serial(serial)
    if not card:
        return _add_cors(request, _err(404, "unknown_card"))
    ack = registry.mark_wiped(cuid, uid)
    if not ack.get("ok"):
        return _add_cors(request, _err(404, ack.get("reason", "unknown_card")))
    cid = card.get("character_id")
    n_mem = character_memory.clear(uid, cid)     # 长期记忆
    _clear_history(uid, cid)                      # 对话历史
    try:
        n_ov = get_user_store().clear_character_override(uid, cid)  # 自定义人格
    except Exception:
        n_ov = False
    return _add_cors(request, _ok({
        "serial": serial,
        "character_id": cid,
        "memories_deleted": n_mem,
        "history_cleared": True,
        "overrides_cleared": bool(n_ov),
    }))


async def handle_card_transfer(request):
    """二手转手：先 wipe，再释放 owner（置空）等下一任碰卡 claim 绑定自己。

    二手场景卖家不知道买家是谁，所以不是"直接转给某人"，而是**释放**。
    未 wipe 就转 → 409 must_wipe_before_transfer：绝不把隐私留给下一任。
    转后清掉自己该角色的会话，防止转手后仍能继续聊。
    """
    uid, err = await _auth(request)
    if err:
        return err
    serial = (request.match_info.get("serial") or "").strip()
    registry = get_card_registry()
    cuid, card = registry.get_by_serial(serial)
    if not card:
        return _add_cors(request, _err(404, "unknown_card"))
    ack = registry.release(cuid, uid)
    if not ack.get("ok"):
        reason = ack.get("reason")
        status = 409 if reason == "must_wipe_before_transfer" else 404
        return _add_cors(request, _err(status, reason))
    cid = card.get("character_id")
    try:
        router = get_character_router()
        sess = router.sessions.get(uid)
        if sess and sess.character_id == cid:
            router.sessions.clear(uid)
    except Exception:
        pass
    return _add_cors(request, _ok({
        "serial": serial,
        "character_id": cid,
        "released": True,
        "lineage_count": ack.get("lineage_count"),
    }))


async def handle_card_collect(request):
    """收藏 / 取消收藏（仅持有者可改）。body: {collected: bool}，默认 true。"""
    uid, err = await _auth(request)
    if err:
        return err
    serial = (request.match_info.get("serial") or "").strip()
    body = await _body(request)
    collected = bool((body or {}).get("collected", True))
    registry = get_card_registry()
    cuid, card = registry.get_by_serial(serial)
    if not card:
        return _add_cors(request, _err(404, "unknown_card"))
    ack = registry.set_collected(cuid, uid, collected)
    if not ack.get("ok"):
        return _add_cors(request, _err(404, ack.get("reason", "unknown_card")))
    return _add_cors(request, _ok({"serial": serial, "collected": ack.get("collected")}))


async def handle_activate(request):
    uid, err = await _auth(request)
    if err:
        return err
    body = await _body(request)
    cid = request.match_info["character_id"].strip()
    source = (body or {}).get("source", CHANNEL_APP)
    if source != CHANNEL_APP:
        # 显式要求 app 面；其余按 app 处理（小程序点开角色必为 app 面）
        source = CHANNEL_APP
    router = get_character_router()
    p = router.profiles.get(cid)
    # owned 分层闸门：未持卡 uid 手动选也进不去（否则可绕过 GET /characters 的分层）
    if not _visible_to(uid, p, get_card_registry()):
        return _add_cors(request, _err(404, "unknown_character"))
    ack = router.on_app_open(uid, cid)
    if not ack.get("ok"):
        return _add_cors(request, _err(404, ack.get("reason", "unknown_character")))
    # 契约 2.2 收敛（纯附加字段，不动已有字段——已提审前端 836554f 零影响）：
    # 苏醒仪式要的过渡自语 + TTS 参数一次带齐，前端不必再翻列表。
    # ritual_type（契约 2.2 出参必填项）：以该 uid 与此角色的**对话历史**判定——
    # 无历史=首次激活（first）；有历史=回归唤醒（wake）。
    # 「换卡 vs 同卡重插」的时长/表现细分由设备端按 DEC-038 判，后端不判。
    # 转让 wipe 清历史 → 下一任主人拿到 first（与「下一任空白」设计一致）。
    ritual_type = "wake" if _get_history(uid, cid) else "first"
    return _add_cors(request, _ok({
        "character_id": ack["character_id"],
        "ritual_type": ritual_type,
        "name": ack.get("name"),
        "channel": ack.get("channel"),
        "device_should_speak": ack.get("device_should_speak"),
        "transition_line": p.transition_line,
        "tts": {
            # 契约 §2.3：用户调校音色优先，回落 profile 出厂值
            "voice_type": _effective_voice(uid, cid, p.voice_type),
            # 未配置 speed 的角色回 null，前端按「用默认」处理
            "speed": p.speed,
            # 与 /api/v1/tts（HTTP 面、无设备连接）实际回落一致：恒 16000。
            # 设备面（conn.sample_rate=24000）5xxxxx/6xxxxx 才拿 24k，
            # 权威逻辑在 tencent.TTSProvider._resolve_sample_rate。
            "sample_rate": 16000,
        },
    }))


async def handle_message(request):
    uid, err = await _auth(request)
    if err:
        return err
    cid = request.match_info["character_id"].strip()
    body = await _body(request)
    content = (body or {}).get("content", "").strip()
    if not content:
        return _add_cors(request, _err(400, "content 必填"))
    router = get_character_router()
    # 角色一致性：对话前确保该角色已激活（activate/tap 已设）；未激活则此处补设 app 面
    sess = router.sessions.get(uid)
    if not sess or sess.character_id != cid:
        # 与 /activate 同闸门：owned 角色未持卡，不能靠「直接发消息」绕进来
        p = router.profiles.get(cid)
        if not _visible_to(uid, p, get_card_registry()):
            return _add_cors(request, _err(404, "unknown_character"))
        ack = router.on_app_open(uid, cid)
        if not ack.get("ok"):
            return _add_cors(request, _err(404, ack.get("reason", "unknown_character")))

    system_prompt = router.build_system_prompt(uid)
    if not system_prompt:
        return _add_cors(request, _err(422, "该角色无 system_prompt，无法对话"))

    # 记忆分层：持卡=长期（检索注入 <memory>），无卡=仅短期滚动窗口（不注入、不抽取）
    tier = character_memory.tier_of(uid, cid)
    system_prompt, _mem_block = character_memory.apply_to_system_prompt(
        system_prompt, uid, cid, query=content)

    llm = _get_llm()
    if llm is None:
        return _add_cors(request, _err(503, "LLM 未就绪"))

    history = _get_history(uid, cid)
    dialogue = _build_dialogue(system_prompt, history, content)
    stream = bool((body or {}).get("stream", False))

    # ── 非流式（默认，向后兼容）：整段返回 ──
    if not stream:
        try:
            # 同步 LLM 流下沉线程池，避免整段等待期卡死事件循环
            reply = await asyncio.to_thread(_collect_no_stream, llm, dialogue)
        except Exception as e:
            logger.warning(f"[character] LLM 调用失败 uid={uid}: {e}")
            return _add_cors(request, _err(502, "LLM 调用失败"))
        if not reply:
            return _add_cors(request, _err(502, "LLM 空回复"))
        _push_history(uid, cid, "user", content)
        _push_history(uid, cid, "assistant", reply)
        character_memory.extract_async(uid, cid, content, reply, llm_getter=_get_llm)
        target = router.route_response(uid, reply)
        # 契约 §2.3：响应 voice 是前端「权威 voice」（直接喂 /tts？voice=），
        # 用户调校音色必须在此出口生效，回落 profile 出厂值。
        target.voice = _effective_voice(uid, cid, target.voice)
        if target.target == CHANNEL_DEVICE:
            did = (router.sessions.get(uid) or object()).device_id if router.sessions.get(uid) else None
            if did:
                try:
                    from fuguoai_core.utils.networked_reminder_scheduler import speak_text_to_device
                    ok = await speak_text_to_device(did, reply, label=f"character {cid}",
                                                    voice=target.voice)
                    if not ok:
                        logger.info(f"[character] device TTS 未成功 did={did}")
                except Exception as e:
                    logger.warning(f"[character] device TTS 异常 did={did}: {e}")
            else:
                logger.info(f"[character] device 面但无 device_id，跳过 TTS")
        return _add_cors(request, _ok({
            "character_id": cid,
            "channel": target.target,
            "reply": reply,
            "voice": target.voice,
            "device_should_speak": target.device_should_speak(),
            "memory_tier": tier,
            "memory_hint": ("" if tier == character_memory.TIER_LONG
                            else character_memory.SHORT_TERM_HINT),
        }))

    # ── 流式（stream:true）：SSE 逐块吐文本，前端逐句 TTS 接力播 ──
    resp = web.StreamResponse()
    resp.headers["Content-Type"] = "text/event-stream; charset=utf-8"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"  # 防 nginx 缓冲 SSE（WS Upgrade 才自动关，SSE 需显式）
    resp.enable_compression(False)
    try:
        await resp.prepare(request)
    except Exception as e:
        logger.warning(f"[character] SSE 准备失败 uid={uid}: {e}")
        return _add_cors(request, _err(502, "流式通道建立失败"))

    full = []
    stop_evt = threading.Event()
    try:
        async for piece in _llm_pieces_async(llm, dialogue, stop_evt):
            full.append(piece)
            try:
                await resp.write(_sse({"delta": piece}))
            except Exception:
                # 客户端断开 / 网络异常：通知 producer 停迭代，静默收流
                logger.info(f"[character] SSE 客户端断开 uid={uid}")
                stop_evt.set()
                return resp
    except Exception as e:
        stop_evt.set()
        logger.warning(f"[character] 流式 LLM 失败 uid={uid}: {e}")
        try:
            await resp.write(_sse({"error": str(e)}))
        except Exception:
            pass
        await resp.write_eof()
        return resp

    reply = "".join(full)
    if not reply:
        try:
            await resp.write(_sse({"error": "LLM 空回复"}))
        except Exception:
            pass
        await resp.write_eof()
        return resp

    _push_history(uid, cid, "user", content)
    _push_history(uid, cid, "assistant", reply)
    character_memory.extract_async(uid, cid, content, reply, llm_getter=_get_llm)

    target = router.route_response(uid, reply)
    # 契约 §2.3（流式出口同非流式）：done 帧 voice 亦为前端权威 voice
    target.voice = _effective_voice(uid, cid, target.voice)
    if target.target == CHANNEL_DEVICE:
        did = (router.sessions.get(uid) or object()).device_id if router.sessions.get(uid) else None
        if did:
            try:
                from fuguoai_core.utils.networked_reminder_scheduler import speak_text_to_device
                ok = await speak_text_to_device(did, reply, label=f"character {cid}",
                                                voice=target.voice)
                if not ok:
                    logger.info(f"[character] device TTS 未成功 did={did}")
            except Exception as e:
                logger.warning(f"[character] device TTS 异常 did={did}: {e}")
        else:
            logger.info(f"[character] device 面但无 device_id，跳过 TTS")

    try:
        await resp.write(_sse({
            "done": True,
            "character_id": cid,
            "channel": target.target,
            "reply": reply,
            "voice": target.voice,
            "device_should_speak": target.device_should_speak(),
            "memory_tier": tier,
            "memory_hint": ("" if tier == character_memory.TIER_LONG
                            else character_memory.SHORT_TERM_HINT),
        }))
    except Exception:
        pass
    await resp.write_eof()
    return resp


async def handle_get_session(request):
    uid, err = await _auth(request)
    if err:
        return err
    router = get_character_router()
    sess = router.sessions.get(uid)
    if not sess or not sess.character_id:
        return _add_cors(request, _ok({"character_id": None, "channel": None, "device_id": None}))
    return _add_cors(request, _ok({
        "character_id": sess.character_id,
        "channel": sess.channel,
        "device_id": sess.device_id,
    }))


async def handle_get_binding(request):
    """GET /api/v1/character-binding?nfc_uid= —— 查卡绑定（技术契约 2.4，测试/运营）。

    脱敏规则（2026-10-16 收紧）：仅 owner 本人或运营白名单见全量 binding；
    其余调用方只见公开字段（character_id/serial/status）——防 uid 枚举与
    他人 tuning/lineage 隐私泄露。
    """
    uid, err = await _auth(request)
    if err:
        return err
    card_uid = (request.query.get("nfc_uid") or "").strip()
    if not card_uid:
        return _add_cors(request, _err(400, "nfc_uid 必填"))
    b = get_card_registry().get(card_uid)
    if not b:
        return _add_cors(request, _err(404, "unknown_card"))
    owner = b.get("owner_uid")
    if (owner and owner == uid) or _is_binding_admin(uid):
        binding = dict(b)
    else:
        binding = {k: b.get(k) for k in ("character_id", "serial", "status")}
    binding["card_uid"] = card_uid  # 测试接口：调用方本就持 UID（入参原样回带）
    return _add_cors(request, _ok({"binding": binding}))


async def handle_put_binding(request):
    """PUT /api/v1/character-binding —— 建/换绑（技术契约 2.4，测试/运营）。

    用途：测试期 mock UID 跑通页面链路，不等真实卡（契约 2.4 即为此设计）。

    权限规则（2026-10-16 收紧，防三个洞）：
    1. **owned-tier 角色仅运营白名单**（config `character_binding.admin_uids`，
       缺省空=对所有人关闭）——防 mock UID 免费解锁锁定角色（知秋/灯灯）；
    2. **已被他人 claim 的 UID 仅本人或白名单可改**——防劫持他人卡映射；
    3. **serial 仅白名单可指定**——防伪造出厂编号，其余自动 MOCK_<uid 尾 6 位>；
    4. owner_uid 本接口一律不设置（领卡只走 /nfc/claim 单通道，归属与映射两条线）。
    """
    uid, err = await _auth(request)
    if err:
        return err
    body = await _body(request)
    card_uid = (body.get("nfc_uid") or "").strip()
    character_id = (body.get("character_id") or "").strip()
    if not card_uid or not character_id:
        return _add_cors(request, _err(400, "nfc_uid/character_id 必填"))
    router = get_character_router()
    # 角色白名单：绝不接受客户端自报未知 character_id（与 /nfc/tap 同闸门）
    p = router.profiles.get(character_id)
    if p is None or not router.profiles.validate_id(character_id):
        return _add_cors(request, _err(400, "unknown_character"))
    admin = _is_binding_admin(uid)
    # 规则 1：owned-tier → 仅白名单（锁定闸门不能被测试接口绕开）
    if _is_owned_tier(p) and not admin:
        return _add_cors(request, _err(403, "admin_only"))
    # 规则 2：已被他人 claim 的卡 → 仅本人或白名单可改
    cur = get_card_registry().get(card_uid)
    cur_owner = (cur or {}).get("owner_uid")
    if cur_owner and cur_owner != uid and not admin:
        return _add_cors(request, _err(403, "not_card_owner"))
    # 规则 3：serial 仅白名单可指定（防伪造出厂编号）
    serial = body.get("serial") if admin else None
    ack = get_card_registry().bind(card_uid, character_id, serial=serial)
    if not ack.get("ok"):
        return _add_cors(request, _err(400, ack.get("reason", "bind_failed")))
    return _add_cors(request, _ok({"binding": ack["binding"]}))


async def handle_clear_session(request):
    uid, err = await _auth(request)
    if err:
        return err
    router = get_character_router()
    sess = router.sessions.get(uid)
    cid = sess.character_id if sess else None
    router.sessions.clear(uid)
    _clear_history(uid, cid)  # cid=None 时清该 uid 全部（兜底）
    return _add_cors(request, _ok({"cleared": True}))


async def handle_options(request):
    return _add_cors(request, web.Response(status=204))


@register_routes
def register(app):
    app.router.add_get("/api/v1/characters", handle_list_characters)
    app.router.add_get("/api/v1/characters/{character_id}", handle_get_character)
    app.router.add_post("/api/v1/nfc/tap", handle_nfc_tap)
    app.router.add_post("/api/v1/nfc/claim", handle_nfc_claim)
    app.router.add_get("/api/v1/cards/me", handle_cards_me)
    app.router.add_get("/api/v1/characters/{character_id}/lineage", handle_character_lineage)
    app.router.add_post("/api/v1/cards/{serial}/wipe", handle_card_wipe)
    app.router.add_post("/api/v1/cards/{serial}/transfer", handle_card_transfer)
    app.router.add_put("/api/v1/cards/{serial}/collect", handle_card_collect)
    app.router.add_post("/api/v1/characters/{character_id}/activate", handle_activate)
    app.router.add_post("/api/v1/characters/{character_id}/messages", handle_message)
    app.router.add_get("/api/v1/session", handle_get_session)
    app.router.add_post("/api/v1/session/clear", handle_clear_session)
    app.router.add_get("/api/v1/character-binding", handle_get_binding)
    app.router.add_put("/api/v1/character-binding", handle_put_binding)
    for p in (
        "/api/v1/characters",
        "/api/v1/characters/{character_id}",
        "/api/v1/nfc/tap",
        "/api/v1/nfc/claim",
        "/api/v1/cards/me",
        "/api/v1/characters/{character_id}/lineage",
        "/api/v1/cards/{serial}/wipe",
        "/api/v1/cards/{serial}/transfer",
        "/api/v1/cards/{serial}/collect",
        "/api/v1/characters/{character_id}/activate",
        "/api/v1/characters/{character_id}/messages",
        "/api/v1/session",
        "/api/v1/session/clear",
        "/api/v1/character-binding",
    ):
        app.router.add_options(p, handle_options)
