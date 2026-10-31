"""NFC 实体卡注册表（模型 b：后端权威白名单，防克隆 + 撑起编号/履历/传承）。

设计要点
--------
- 实体卡只携带一个只读 ``card_uid``；本注册表是「card_uid → character_id + 编号
  (serial) + 持有者 (owner_uid) + 传承链 (lineage)」的**唯一权威来源**，
  绝不接受客户端自报 character_id（那会重新打开"空白卡写已知 id"的克隆口）。
- ``owner_uid`` 仅在「领卡 claim」时绑定一次；**日常碰卡激活不改 owner**
  ——这是"陌生人捡到已领的卡也占不走"的守卫，也是丢卡不丢数据的前提之一。
- 记忆 key 一律用微信 uid（**非** card_uid），故丢卡只丢"碰一下"的便利、不丢数据
  （详见《天使心分层后端方案_设计稿》§八）。
- 二手转手：P3 的 transfer 会先 wipe 该卡全部聊天+记忆，再清空 owner_uid；
  下一任走 claim 重新绑定自己 → 保证"前主人隐私不留给下一任"。
- 落 ``data/card_registry.json``（bind mount、data/ 已被 gitignore、admin 种子），
  与 characters.yaml 同管理形态。

持久化与 CharacterSessionStore / NetworkedReminderStore 同范式：原子写
（tmp + os.replace）+ 全异常吞掉，绝不因注册表问题拖垮服务启动。
"""

import json
import os
import threading
import time
from typing import Optional

from fuguoai_config.logger import setup_logging

TAG = __name__
logger = setup_logging()

_DATA_DIR = os.environ.get("FUGUOAI_DATA_DIR", "/opt/fuguoai-server/data")
_CARD_REGISTRY_PATH = os.path.join(_DATA_DIR, "card_registry.json")


def _norm_uid(uid):
    """统一 card_uid 形态：去空白/冒号/连字符/下划线，转大写。

    不同读卡端产出的十六进制形态并不一致——Android 走 getNfcA().getUid()、
    iOS 走 onDiscovered 的 res.id、PC 用 USB 读卡器（PN532/ACR122U）导出，
    大小写与分隔符都可能不同。登记与读取必须归一化，否则**同一张物理卡会被判成
    unknown_card**（表现为"我的卡突然用不了"）。
    """
    s = str(uid or "").strip().upper()
    for ch in (" ", ":", "-", "_"):
        s = s.replace(ch, "")
    return s


class CardRegistry:
    """card_uid -> {character_id, serial, owner_uid, claimed_at, created_at, lineage, status}。"""

    def __init__(self, path=None):
        self._path = path or _CARD_REGISTRY_PATH
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def _load(self) -> dict:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        except Exception as e:  # 任何异常都按空库处理，绝不拖垮启动
            logger.bind(tag=TAG).warning(f"加载卡注册表失败，按空处理: {e}")
            return {}
        cards = raw.get("cards", {}) if isinstance(raw, dict) else {}
        if not isinstance(cards, dict):
            return {}
        # 键归一化：登记端（PC 读卡器导出）与读卡端（手机）形态可能不一致
        return {_norm_uid(k): v for k, v in cards.items()}

    def _save(self, cards: dict):
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"cards": cards}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception as e:
            logger.bind(tag=TAG).warning(f"写卡注册表失败(跳过持久化): {e}")

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get(self, card_uid: str) -> Optional[dict]:
        return self._load().get(_norm_uid(card_uid))

    def resolve_character(self, card_uid: str) -> Optional[str]:
        """card_uid -> character_id；未注册返回 None。"""
        c = self.get(card_uid)
        if not c:
            return None
        cid = (c.get("character_id") or "").strip()
        return cid or None

    def owns(self, uid: str, character_id: str) -> bool:
        """该 uid 是否持有对应 character_id 的实体卡（owned 分层判定）。"""
        if not uid or not character_id:
            return False
        for c in self._load().values():
            if c.get("character_id") == character_id and c.get("owner_uid") == uid:
                return True
        return False

    def owned_character_ids(self, uid: str) -> set:
        """该 uid 持有的全部 character_id（用于 GET /characters 分层）。"""
        if not uid:
            return set()
        return {c.get("character_id") for c in self._load().values()
                if c.get("owner_uid") == uid and c.get("character_id")}

    def list_by_uid(self, uid: str) -> list:
        """该 uid 持有的全部卡（含 card_uid/serial/lineage），供「我的卡」视图。"""
        out = []
        for cuid, c in self._load().items():
            if c.get("owner_uid") == uid:
                d = dict(c)
                d["card_uid"] = cuid
                out.append(d)
        return out

    def get_by_serial(self, serial: str):
        """按对外编号 serial 查卡，返回 (card_uid, card)；未找到返回 (None, None)。

        对外一律用 serial（human 编号）寻址，**不向客户端暴露 card_uid**
        ——card_uid 是防克隆的关键，只能由 NFC 硬件读出后上报。
        """
        s = (serial or "").strip()
        if not s:
            return None, None
        for cuid, c in self._load().items():
            if (c.get("serial") or "").strip() == s:
                return cuid, c
        return None, None

    def list_all(self) -> list:
        """admin 全量列举（运营后台用）。

        返回的每条**不含 card_uid 明文**——card_uid 是防克隆关键，只由 NFC 硬件读出
        后上报，绝不随 list 下发；对外一律以 serial 作为寻址主键，card_uid 仅在后端
        由 serial 内部解析（见 get_by_serial）。
        """
        out = []
        for cuid, c in self._load().items():
            d = dict(c)
            d["serial"] = (c.get("serial") or "").strip() or f"MOCK_{cuid[-6:]}"
            out.append(d)
        # 未编号的 MOCK 沉底，其余按编号升序
        out.sort(key=lambda x: (x["serial"].startswith("MOCK_"), x["serial"]))
        return out

    # ------------------------------------------------------------------
    # 领卡（一次性绑定 owner）
    # ------------------------------------------------------------------
    def claim(self, card_uid: str, uid: str, now: float = 0.0) -> dict:
        """领卡：仅当 owner_uid 为空时绑定；已绑则拒绝（防占卡）。

        返回 {"ok":True,...} 或 {"ok":False,"reason": unknown_card|already_claimed|
        card_uid_required|uid_required}。
        """
        cuid = _norm_uid(card_uid)
        if not cuid:
            return {"ok": False, "reason": "card_uid_required"}
        if not uid:
            return {"ok": False, "reason": "uid_required"}
        with self._lock:
            cards = self._load()
            c = cards.get(cuid)
            if not c:
                return {"ok": False, "reason": "unknown_card"}
            if c.get("owner_uid"):
                return {"ok": False, "reason": "already_claimed"}
            c["owner_uid"] = uid
            c["claimed_at"] = now or time.time()
            lineage = c.get("lineage") or []
            if uid not in lineage:
                lineage.append(uid)
            c["lineage"] = lineage
            cards[cuid] = c
            self._save(cards)
        logger.bind(tag=TAG).info(
            f"[card] 领卡成功 card={cuid} uid={uid} cid={c.get('character_id')}"
        )
        return {"ok": True, "card_uid": cuid,
                "character_id": c.get("character_id"),
                "serial": c.get("serial")}

    # ------------------------------------------------------------------
    # 二手转手：wipe（前主人一键清空）→ release（释放 owner，等下一任 claim）
    # ------------------------------------------------------------------
    def mark_wiped(self, card_uid: str, uid: str, now: float = 0.0) -> dict:
        """前主人一键清空后打标记。**transfer 前置要求此标记存在**。

        数据归属说明：聊天/长期记忆本来就按微信 uid 存，下一任（不同 uid）天然看不到
        上一任的数据；wipe 是为了让前主人**彻底断开、不留残留**，是"干净分手"而不
        是"防下一任偷看"。
        """
        cuid = _norm_uid(card_uid)
        with self._lock:
            cards = self._load()
            c = cards.get(cuid)
            if not c:
                return {"ok": False, "reason": "unknown_card"}
            if c.get("owner_uid") != uid:
                # 不是自己的卡：统一 unknown_card，不泄露"这张卡存在"
                return {"ok": False, "reason": "unknown_card"}
            c["wiped_at"] = now or time.time()
            cards[cuid] = c
            self._save(cards)
        return {"ok": True, "card_uid": cuid, "wiped_at": c["wiped_at"]}

    def release(self, card_uid: str, uid: str, now: float = 0.0) -> dict:
        """转让（二手卖出）：释放 owner_uid 置空，等下一任碰卡 claim 绑定自己。

        二手场景卖家并不知道买家是谁，所以不是"直接转给某人"，而是**释放**。
        lineage 保留历届持有者 → 这就是"有履历"。
        前置：必须先 wipe（wiped_at 存在），否则拒绝——绝不把隐私留给下一任。
        """
        cuid = _norm_uid(card_uid)
        with self._lock:
            cards = self._load()
            c = cards.get(cuid)
            if not c:
                return {"ok": False, "reason": "unknown_card"}
            if c.get("owner_uid") != uid:
                return {"ok": False, "reason": "unknown_card"}
            if not c.get("wiped_at"):
                return {"ok": False, "reason": "must_wipe_before_transfer"}
            lineage = c.get("lineage") or []
            if uid not in lineage:
                lineage.append(uid)
            c["lineage"] = lineage
            c["owner_uid"] = None        # 释放，等下一任 claim
            c["transferred_at"] = now or time.time()
            c["status"] = "active"
            cards[cuid] = c
            self._save(cards)
        logger.bind(tag=TAG).info(
            f"[card] 转让完成(已释放) serial={c.get('serial')} 历任={len(c['lineage'])}"
        )
        return {"ok": True, "card_uid": cuid, "serial": c.get("serial"),
                "character_id": c.get("character_id"),
                "lineage_count": len(c["lineage"])}

    def admin_release(self, card_uid: str, now: float = 0.0) -> dict:
        """admin 强制释放（运营后台「转让/解绑」）：清空 owner_uid，等下一任 claim。

        与用户态 release() 的区别：跳过 must_wipe_before_transfer 闸门——admin 已在后台
        确认操作意图，无需经消费者 App 的 wipe 流程；但仍保留 lineage 履历、置
        transferred_at、status=active，保证「有履历、可被重新领用」。
        """
        cuid = _norm_uid(card_uid)
        with self._lock:
            cards = self._load()
            c = cards.get(cuid)
            if not c:
                return {"ok": False, "reason": "unknown_card"}
            prev = c.get("owner_uid")
            lineage = c.get("lineage") or []
            if prev and prev not in lineage:
                lineage.append(prev)
            c["lineage"] = lineage
            c["owner_uid"] = None
            c["transferred_at"] = now or time.time()
            c["status"] = "active"
            cards[cuid] = c
            self._save(cards)
        logger.bind(tag=TAG).info(
            f"[card] admin 强制释放 serial={c.get('serial')} 原持有={prev}"
        )
        return {"ok": True, "card_uid": cuid, "serial": c.get("serial"),
                "character_id": c.get("character_id"),
                "previous_owner": prev}

    def set_collected(self, card_uid: str, uid: str, collected: bool = True) -> dict:
        """收藏/取消收藏（仅持有者可改）。"""
        cuid = _norm_uid(card_uid)
        with self._lock:
            cards = self._load()
            c = cards.get(cuid)
            if not c:
                return {"ok": False, "reason": "unknown_card"}
            if c.get("owner_uid") != uid:
                return {"ok": False, "reason": "unknown_card"}
            c["collected"] = bool(collected)
            cards[cuid] = c
            self._save(cards)
        return {"ok": True, "card_uid": cuid, "collected": c["collected"]}

    def bind(self, card_uid: str, character_id: str, serial: Optional[str] = None,
             now: float = 0.0) -> dict:
        """建/换绑（测试/运营接口，技术契约 2.4）：card_uid → character_id 映射。

        - 未登记 UID：新建映射（serial 缺省自动生成 ``MOCK_<uid 尾 6 位>``，
          status=registered，owner 空——等 claim）。
        - 已登记 UID：换 character_id，**不动 owner_uid/lineage/claimed_at**
          （claim 归属是另一条线；日常映射调整不重置持有关系）。
        角色白名单校验在调用方（character_routes）做，本类只管卡映射。
        返回 {"ok":True,"binding":{...}} 或 {"ok":False,"reason":...}。
        """
        cuid = _norm_uid(card_uid)
        cid = (character_id or "").strip()
        if not cuid:
            return {"ok": False, "reason": "card_uid_required"}
        if not cid:
            return {"ok": False, "reason": "character_id_required"}
        with self._lock:
            cards = self._load()
            c = cards.get(cuid)
            if c is None:
                c = {
                    "character_id": cid,
                    "serial": (serial or "").strip() or f"MOCK_{cuid[-6:]}",
                    "owner_uid": None,
                    "claimed_at": None,
                    "created_at": now or time.time(),
                    "lineage": [],
                    "status": "registered",
                }
            else:
                c["character_id"] = cid
                if (serial or "").strip():
                    c["serial"] = serial.strip()
            cards[cuid] = c
            self._save(cards)
        out = dict(c)
        out["card_uid"] = cuid
        logger.bind(tag=TAG).info(f"[card] 建/换绑 card={cuid} -> {cid} serial={c['serial']}")
        return {"ok": True, "binding": out}


_card_registry = None


def get_card_registry() -> CardRegistry:
    global _card_registry
    if _card_registry is None:
        _card_registry = CardRegistry()
    return _card_registry
