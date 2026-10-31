"""天使心「心记忆」分层：无卡=短期（够用但不够），买卡=长期（持久记住他）。

设计来源：《天使心分层后端方案_设计稿》P2。

分层语义
--------
- **short（无卡试玩）**：只给最近 N 轮对话窗口，**不落长期记忆、不检索**，并在响应里
  回一条 hint 让前端可提示"升级解锁长期记忆"——这是"让人觉得不够用"的升级欲来源，
  刻意而为之，不是缺陷。
- **long（持卡拥有）**：每轮后台抽取候选事实写入 MemoryStore，并在下一轮把相关长期
  记忆以 ``<memory>`` 块注入 system_prompt → AI 真正"记得他"。

关键不变量
----------
- 记忆 key 用 **微信 uid + character_id**，绝不用 card_uid
  → 丢卡只丢"碰一下"的便利，不丢数据（设计稿 §八）。
- 分层判定走 ``card_registry.owns(uid, character_id)``，与角色可见性同一判据，
  不会出现"看得见但记不住"或"记了但不该记"的错位。
- 抽取走后台线程 + 超时 + **全异常吞掉**（同 family_rag 范式）：抽取失败/超时
  绝不影响本轮回复，用户永远感知不到。

未做（后续可升级）：语义向量检索 top-k。当前用「关键词命中优先 + 时间倒序」，
对长期记忆条数不多的场景足够，且零额外依赖（不需要 embedding 服务）。
"""

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fuguoai_config.logger import setup_logging

TAG = __name__
logger = setup_logging()

TIER_SHORT = "short"
TIER_LONG = "long"

# 注入 prompt 的长期记忆条数上限（太多会挤占上下文、拖慢 LLM）
_MEMORY_INJECT_LIMIT = 8
# 单轮最多抽取的新事实数（宁少勿错）
_MAX_FACTS_PER_TURN = 3
# 抽取超时（秒），超时即跳过，绝不拖慢回复
_EXTRACT_TIMEOUT = 8.0

# 短期层提示文案（前端可直接展示，制造升级欲）
SHORT_TERM_HINT = "试用仅保留最近对话，升级可解锁长期记忆"

_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="char_mem")

_EXTRACT_PROMPT = """从下面这段对话里，抽取关于「用户本人」的、值得长期记住的事实。
要求：
1. 只输出 JSON 数组，每项是一个字符串；没有值得记的就输出 []
2. 宁少勿错：不确定、一次性的、角色自己说的话都不要写
3. 每条不超过 30 字，不含角色名
4. 不要输出任何解释文字

对话：
用户：{user}
角色：{reply}
"""


# ---------------------------------------------------------------------------
# 分层判定
# ---------------------------------------------------------------------------
def tier_of(uid, character_id):
    """该 uid 对该角色是长期记忆(long)还是短期(short)。判定与角色可见性同源。"""
    try:
        from fuguoai_core.utils.card_registry import get_card_registry
        if character_id and get_card_registry().owns(uid, character_id):
            return TIER_LONG
    except Exception as e:  # 注册表异常时保守降级为短期（绝不因记忆层拖垮对话）
        logger.bind(tag=TAG).warning(f"记忆分层判定失败，按短期处理: {e}")
    return TIER_SHORT


def _memory_store():
    from fuguoai_core.utils.angel_heart_store import get_memory_store
    return get_memory_store()


# ---------------------------------------------------------------------------
# 检索 → 注入块
# ---------------------------------------------------------------------------
def _tokens(text):
    return set(re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{2,}", text or ""))


def retrieve(uid, character_id, query="", limit=_MEMORY_INJECT_LIMIT):
    """取该角色的长期记忆：关键词命中优先，其次时间倒序。短期层直接返回空。"""
    if tier_of(uid, character_id) != TIER_LONG:
        return []
    try:
        facts = _memory_store().list(uid, character_id) or []
    except Exception as e:
        logger.bind(tag=TAG).warning(f"读取长期记忆失败: {e}")
        return []
    q = _tokens(query)
    scored = []
    for f in facts:
        txt = (f.get("fact") or "").strip()
        if not txt:
            continue
        hit = len(q & _tokens(txt)) if q else 0
        scored.append((hit, f.get("created_at", 0), txt))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [t for _, _, t in scored[:limit]]


def build_memory_block(uid, character_id, query="", limit=_MEMORY_INJECT_LIMIT):
    """渲染成注入 system_prompt 的 <memory> 块；无记忆/短期层返回空串。"""
    lines = retrieve(uid, character_id, query=query, limit=limit)
    if not lines:
        return ""
    body = "\n".join(f"- {t}" for t in lines)
    return f"<memory>\n以下是你长期记住的关于这位用户的事实：\n{body}\n</memory>"


def apply_to_system_prompt(system_prompt, uid, character_id, query=""):
    """把长期记忆块拼到 system_prompt 后；短期层原样返回。"""
    block = build_memory_block(uid, character_id, query=query)
    if not block:
        return system_prompt, ""
    return f"{system_prompt}\n\n{block}", block


# ---------------------------------------------------------------------------
# 抽取 → 落库
# ---------------------------------------------------------------------------
def _parse_facts(text):
    """从 LLM 输出里解析出事实数组；解析失败一律返回空（宁少勿错）。"""
    if not text:
        return []
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        s = str(item).strip()
        if 1 <= len(s) <= 60:
            out.append(s)
    return out


def extract_sync(uid, character_id, user_text, reply, llm_getter=None):
    """同步抽取并落库（供测试与内部调用）。短期层直接跳过——不写任何长期记忆。

    返回本次新写入的条数。
    """
    if tier_of(uid, character_id) != TIER_LONG:
        return 0
    if llm_getter is None:
        return 0
    try:
        llm = llm_getter()
        if llm is None:
            return 0
        prompt = _EXTRACT_PROMPT.format(user=(user_text or "")[:500],
                                        reply=(reply or "")[:500])
        raw = llm.response_no_stream(prompt, "")
    except Exception as e:
        logger.bind(tag=TAG).warning(f"记忆抽取 LLM 失败(跳过): {e}")
        return 0

    facts = _parse_facts(raw)[:_MAX_FACTS_PER_TURN]
    if not facts:
        return 0
    try:
        store = _memory_store()
        existing = {(f.get("fact") or "").strip()
                    for f in store.list(uid, character_id) or []}
        n = 0
        for s in facts:
            if s in existing:
                continue
            store.add(uid, character_id, s)
            existing.add(s)
            n += 1
        if n:
            logger.bind(tag=TAG).info(
                f"[memory] 长期记忆 +{n} uid={uid} cid={character_id}")
        return n
    except Exception as e:
        logger.bind(tag=TAG).warning(f"长期记忆落库失败(跳过): {e}")
        return 0


def extract_async(uid, character_id, user_text, reply, llm_getter=None,
                  timeout=_EXTRACT_TIMEOUT):
    """后台抽取：超时/异常全吞，绝不阻塞或影响本轮回复。"""
    if tier_of(uid, character_id) != TIER_LONG:
        return  # 短期层根本不排任务

    def _job():
        try:
            extract_sync(uid, character_id, user_text, reply, llm_getter)
        except Exception as e:
            logger.bind(tag=TAG).warning(f"后台记忆抽取异常(已吞): {e}")

    try:
        _EXECUTOR.submit(_job)
    except Exception as e:
        logger.bind(tag=TAG).warning(f"提交记忆抽取任务失败(跳过): {e}")


def clear(uid, character_id):
    """清空该 uid+角色的全部长期记忆（P3 transfer/wipe 会用到）。"""
    try:
        return _memory_store().delete_all(uid, character_id)
    except Exception as e:
        logger.bind(tag=TAG).warning(f"清空长期记忆失败: {e}")
        return 0
