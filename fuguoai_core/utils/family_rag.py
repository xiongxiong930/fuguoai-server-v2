"""家庭 RAG 知识库（创新维度⑤）。

真进化动机：复果AI 此前只有"对话摘要"记忆（mem_local_short / MemoryRAG），
没有"家庭客观事实"这一层。用户说"我家猫叫咪咪""爸爸对花粉过敏"，这些事实
散落在对话里无法结构化召回。本模块让设备"记住家里的事"并在相关对话语义注入：

  - 对话自动抽取（三段式后台 LLM，零手动录入）：每轮首层对话后抽取家庭事实；
  - 可插拔 embedding：LocalHashEmbedding（零依赖降级）/ OpenAIEmbedding（远程真语义）/ LocalSTEmbedding（本地 sentence-transformers 真语义，离线隐私）；
  - 向量检索 top-k 注入 <family_knowledge> 块，独立于记忆通道；
  - 相关度闸门（min_score / rel_gap，默认 0 = 关闭）：挡住"无关问句也把家庭事实塞进 prompt"的噪声注入；
  - 全异常吞掉，绝不阻塞主对话；存储原子写，维度不匹配安全跳过。

与维度②(persona)、维度④(dialogue_eval) 同范式：专用线程池 + 超时熔断 + 单例。
"""

import asyncio
import copy
import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from fuguoai_config.config_loader import get_project_dir
from fuguoai_config.logger import setup_logging
from fuguoai_core.providers.embedding import create_embedding_instance
from fuguoai_core.utils.llm import create_instance
from fuguoai_core.utils.metrics import METRICS
from fuguoai_core.utils.util import normalize_device_id

TAG = __name__
logger = setup_logging()

_CATEGORIES = {
    "family",
    "pet",
    "habit",
    "schedule",
    "preference",
    "item",
    "health",
    "other",
}

_EXTRACT_PROMPT = """你是一个家庭信息抽取器。请从下面的"用户提问"和"AI回答"中，抽取出关于这个家庭的客观事实（成员、宠物、习惯、日程、偏好、物品位置、健康状况等）。
只输出一个 JSON 数组，每个元素为 {{"content":"事实陈述","category":"类别","tags":["关键词"]}}。
若没有任何家庭客观事实，输出空数组 []。
类别从以下选一：family(家庭成员)/pet(宠物)/habit(习惯)/schedule(日程)/preference(偏好)/item(物品位置)/health(健康)/other。
用户提问：{user}
AI回答：{assistant}
输出："""


def _parse_facts(text):
    """鲁棒解析抽取输出为事实 list；非法/缺失整体降级为 []。"""
    if not text:
        return []
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except Exception:
        return []
    if not isinstance(arr, list):
        return []
    out = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        content = (item.get("content") or "").strip()
        if len(content) < 2:
            continue
        cat = item.get("category") or "other"
        if cat not in _CATEGORIES:
            cat = "other"
        tags = item.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        if not isinstance(tags, list):
            tags = []
        out.append({"content": content, "category": cat, "tags": [str(t) for t in tags][:8]})
    return out


class FamilyRAG:
    def __init__(self, config):
        cfg = (config.get("FamilyRAG", {}) or {}) if isinstance(config, dict) else {}
        self.enabled = bool(cfg.get("enabled", False))
        self.top_k = int(cfg.get("top_k", 5))
        emb_cfg = (cfg.get("embedding") or {}) or {}
        emb_type = emb_cfg.get("type", "local")
        try:
            self.embedding = create_embedding_instance(emb_type, emb_cfg)
        except Exception as e:
            logger.bind(tag=TAG).warning(f"embedding 初始化失败，降级 local: {e}")
            self.embedding = create_embedding_instance("local", {})
        # 当前 provider 标识：用于向量迁移（切换后端时对已存事实重算 vec）
        self._emb_type = getattr(self.embedding, "type_key", emb_type)
        self._migrate_lock = threading.Lock()
        self._migrate_ts = {}
        # 相关度闸门（默认 0 = 不过滤，保持旧行为；阈值由配置显式开启）：
        # 不同 embedding 后端的余弦分布差异极大，硬编码阈值会误伤，故默认关。
        # 实测（bge-small-zh-v1.5，24 条相关问句 + 20 条无关问句）：
        #   相关 top1 min=0.481 / 中位=0.674；无关 top1 max=0.389 / 中位=0.294
        #   ⇒ min_score=0.40 时召回 24/24 且无关泄漏 0/20 ⇒ 生产配 0.40，rel_gap=0.12。
        self.min_score = float(cfg.get("min_score", 0.0) or 0.0)
        # 相对衰减：仅保留不显著逊于 top1 的条目（top1 - rel_gap 为下限），
        # 避免"一条准 + 四条陪跑噪声"一起进 prompt。0 = 不启用。
        self.rel_gap = float(cfg.get("rel_gap", 0.0) or 0.0)
        ext = (cfg.get("extract") or {}) or {}
        self.extract_enabled = bool(ext.get("enabled", True))
        self.extract_depth = int(ext.get("trigger_depth", 0))
        self.extract_timeout = float(ext.get("timeout", 8.0))
        self.extract_llm_block = (
            ext.get("llm")
            or (config.get("selected_module", {}) or {}).get("LLM")
            or "OpenAILLM"
        )
        self._dir = os.path.join(get_project_dir(), "data", "family_rag")
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="famrag")
        # 加固：内存缓存 + 异步落盘。锁只护内存变更，磁盘 IO 全移出事件循环，
        # 避免慢盘持锁冻结 loop 触发看门狗自杀（旧实现在持锁内直接 _load/_save 做阻塞磁盘 IO）。
        self._mem = {}
        # RLock（可重入）：upsert/remove/clear 在持锁内会调用 _get_mem（同样取此锁），须可重入否则自锁死锁。
        self._io_lock = threading.RLock()
        self._test_llm = None
        self._llm_cache = {}
        self._llm_lock = threading.Lock()

    # ---- 测试注入点 ----
    def _set_test_llm(self, inst):
        self._test_llm = inst

    # ---- 存储 ----
    def _path(self, device_id):
        did = normalize_device_id(device_id)
        return os.path.join(self._dir, f"{did}.json")

    def _load(self, device_id):
        p = self._path(device_id)
        try:
            if not os.path.exists(p):
                return {"facts": []}
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {"facts": []}
            return data
        except Exception as e:
            logger.bind(tag=TAG).warning(f"家庭知识读取失败(降级空): {e}")
            return {"facts": []}

    def _save(self, device_id, data):
        p = self._path(device_id)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, p)

    # ---- 内存缓存 + 异步落盘（加固：磁盘 IO 不再持锁阻塞事件循环） ----
    def _get_mem(self, did):
        """取内存缓存(没有则从磁盘加载一次)。锁只护内存；冷 miss 的磁盘读每设备每进程仅一次。"""
        with self._io_lock:
            data = self._mem.get(did)
            if data is None:
                data = self._load(did)
                self._mem[did] = data
            return data

    # ---- 录入（内部/抽取调用） ----
    def add_fact(self, device_id, content, category="", tags=None):
        """录入/更新一条家庭事实（供对话抽取调用）。返回是否成功写入。"""
        return self.upsert_fact(device_id, content, category, tags) is not None

    # ---- 管理 API 友好方法（小程序主动录入/管理） ----
    @staticmethod
    def _clean_fact(f):
        """去掉向量，返回可对外暴露的条目。"""
        return {
            "id": f.get("id"),
            "content": f.get("content", ""),
            "category": f.get("category", "other"),
            "tags": list(f.get("tags", [])),
            "ts": f.get("ts", 0),
            "weight": f.get("weight", 1.0),
        }

    def list_facts(self, device_id):
        """返回该设备全部家庭知识条目（不含向量），按录入时间倒序。任何异常返回 []。"""
        try:
            data = self._get_mem(normalize_device_id(device_id))
            facts = [self._clean_fact(f) for f in data.get("facts", [])]
            facts.sort(key=lambda x: x.get("ts", 0), reverse=True)
            return facts
        except Exception:
            return []

    def upsert_fact(self, device_id, content, category="", tags=None):
        """主动录入/更新一条家庭事实。返回完整条目 dict（含 id）或 None（失败/内容过短）。

        相同 content 的 sha1 相同 => 视为同一条，更新其 category/tags/vec/ts/weight（不新增副本）；
        不同 content 各自独立。供小程序「家庭知识」管理页调用。
        """
        did = normalize_device_id(device_id)
        if not self.enabled:
            return None
        content = (content or "").strip()
        if len(content) < 2:
            return None
        fid = hashlib.sha1(content.encode("utf-8")).hexdigest()[:16]
        try:
            vec = self.embedding.embed([content])[0]
        except Exception as e:
            logger.bind(tag=TAG).warning(f"embedding 失败(跳过录入): {e}")
            return None
        if not vec or all(float(x) == 0.0 for x in vec):
            logger.bind(tag=TAG).warning("embedding 返回零向量(跳过录入)")
            return None
        cat = (category or "other")
        if cat not in _CATEGORIES:
            cat = "other"
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        tags = list(tags or [])
        now = int(time.time())
        try:
            with self._io_lock:
                data = self._get_mem(did)
                facts = data.setdefault("facts", [])
                existing = next((f for f in facts if f.get("id") == fid), None)
                if existing:
                    existing["ts"] = now
                    existing["weight"] = min(2.0, float(existing.get("weight", 1.0)) + 0.1)
                    existing["category"] = cat
                    existing["tags"] = list(tags)
                    existing["vec"] = vec
                    existing["emb_type"] = self._emb_type
                    fact = existing
                else:
                    fact = {
                        "id": fid,
                        "content": content,
                        "category": cat,
                        "tags": list(tags),
                        "vec": vec,
                        "emb_type": self._emb_type,
                        "ts": now,
                        "weight": 1.0,
                    }
                    facts.append(fact)
                snapshot = copy.deepcopy(data)
            # 锁外做磁盘写：避免持锁做阻塞 IO 冻结事件循环（原实现在锁内 _load/_save）。
            # 读路径已改为内存缓存(_get_mem)，热路径不再碰磁盘。
            self._save(did, snapshot)
            try:
                METRICS.set_gauge(
                    "fuguoai_family_rag_facts",
                    len(facts),
                    labels={"device_id": did},
                )
            except Exception:
                pass
            return self._clean_fact(fact)
        except Exception as e:
            logger.bind(tag=TAG).warning(f"家庭知识写入失败: {e}")
            return None

    def remove_fact(self, device_id, fact_id):
        """按 id 删除一条家庭知识；返回是否实际删除。"""
        if not fact_id:
            return False
        did = normalize_device_id(device_id)
        try:
            with self._io_lock:
                data = self._get_mem(did)
                facts = data.get("facts", [])
                new = [f for f in facts if f.get("id") != fact_id]
                if len(new) == len(facts):
                    return False
                data["facts"] = new
                snapshot = copy.deepcopy(data)
            # 锁外做磁盘写：避免持锁做阻塞 IO 冻结事件循环
            self._save(did, snapshot)
            try:
                METRICS.set_gauge(
                    "fuguoai_family_rag_facts",
                    len(new),
                    labels={"device_id": did},
                )
            except Exception:
                pass
            return True
        except Exception as e:
            logger.bind(tag=TAG).warning(f"家庭知识删除失败: {e}")
            return False

    def clear(self, device_id):
        """清空该设备全部家庭知识；返回删除条数。"""
        did = normalize_device_id(device_id)
        try:
            with self._io_lock:
                data = self._get_mem(did)
                n = len(data.get("facts", []))
                data["facts"] = []
                snapshot = copy.deepcopy(data)
            # 锁外做磁盘写：避免持锁做阻塞 IO 冻结事件循环
            self._save(did, snapshot)
            try:
                METRICS.set_gauge(
                    "fuguoai_family_rag_facts", 0, labels={"device_id": did}
                )
            except Exception:
                pass
            return n
        except Exception as e:
            logger.bind(tag=TAG).warning(f"家庭知识清空失败: {e}")
            return 0

    # ---- 向量迁移（切换 embedding 后端时） ----
    def _ensure_vectors(self, device_id, data):
        """向量迁移：若某条事实的 emb_type 与当前 provider 不符（切换了 embedding 后端），

        就地用当前 provider 重算向量并落盘。provider 暂不可用（零向量）则跳过、下次重试，
        绝不破坏既有数据、绝不无限循环。返回（可能已更新的）data 引用。绝不抛异常。
        """
        facts = data.get("facts", []) if isinstance(data, dict) else []
        if not facts:
            return data
        now = time.time()
        with self._migrate_lock:
            last = self._migrate_ts.get(device_id, 0)
            if now - last < 60:  # 节流：provider 不可用时不每查重试
                return data
            self._migrate_ts[device_id] = now
        changed = False
        for f in facts:
            if f.get("emb_type") == self._emb_type:
                continue
            content = (f.get("content") or "").strip()
            if not content:
                continue
            try:
                vec = self.embedding.embed([content])[0]
            except Exception as e:
                logger.bind(tag=TAG).warning(f"向量迁移 embed 失败(跳过): {e}")
                vec = None
            if not vec or all(float(v) == 0.0 for v in vec):
                continue
            f["vec"] = vec
            f["emb_type"] = self._emb_type
            changed = True
        if changed:
            try:
                with self._io_lock:
                    snapshot = copy.deepcopy(data)
                # 锁外做磁盘写
                self._save(device_id, snapshot)
            except Exception as e:
                logger.bind(tag=TAG).warning(f"向量迁移落盘失败: {e}")
        return data

    # ---- 检索 ----
    @staticmethod
    def _cosine_topk(query_vec, facts, top_k, min_score=0.0, rel_gap=0.0):
        """余弦 top-k。min_score/rel_gap 默认 0 = 不过滤（旧行为，零回归）。

        返回保留的事实列表（按相似度降序，已截断 top_k）。
        """
        q = np.asarray(query_vec, dtype=np.float32)
        nq = float(np.linalg.norm(q))
        if nq == 0:
            return []
        q = q / nq
        scored = []
        for f in facts:
            fv = f.get("vec")
            if not fv or len(fv) != len(query_vec):
                continue
            v = np.asarray(fv, dtype=np.float32)
            nv = float(np.linalg.norm(v))
            if nv == 0:
                continue
            v = v / nv
            sim = float(np.dot(q, v))
            scored.append((sim, f))
        # 排除零重叠（完全不相关）的事实，避免噪声注入 prompt
        scored = [(s, f) for s, f in scored if s > 1e-9]
        scored.sort(key=lambda x: x[0], reverse=True)
        if not scored:
            return []
        # 相关度闸门：绝对下限 min_score 挡噪声；相对衰减 rel_gap 挡"远逊于 top1"的陪跑项。
        thr = float(min_score or 0.0)
        if rel_gap and rel_gap > 0:
            thr = max(thr, scored[0][0] - float(rel_gap))
        if thr <= 0:
            return [f for _, f in scored[:top_k]]
        kept = [f for s, f in scored if s >= thr]
        return kept[:top_k]

    def retrieve(self, device_id, query):
        """语义检索 top-k 家庭事实内容。任何异常返回 []。"""
        if not self.enabled or not query:
            return []
        did = normalize_device_id(device_id)
        data = self._get_mem(did)
        data = self._ensure_vectors(did, data)
        facts = data.get("facts", [])
        if not facts:
            return []
        try:
            qvec = self.embedding.embed([query])[0]
        except Exception:
            return []
        top = self._cosine_topk(qvec, facts, self.top_k, self.min_score, self.rel_gap)
        try:
            METRICS.inc(
                "fuguoai_family_rag_retrieves_total", labels={"device_id": did}
            )
            # 被相关度闸门挡掉的条数（用于线上调优阈值：持续 >0 且召回不足则需下调）
            _dropped = max(0, len(facts) - len(top))
            if _dropped:
                METRICS.inc(
                    "fuguoai_family_rag_filtered_total",
                    value=float(_dropped),
                    labels={"device_id": did},
                )
        except Exception:
            pass
        return [f["content"] for f in top]

    def render(self, device_id, query):
        """渲染 <family_knowledge> 块；未启用/无相关事实返回空串。"""
        if not self.enabled:
            return ""
        try:
            facts = self.retrieve(device_id, query)
            if not facts:
                return ""
            block = "<family_knowledge>\n" + "\n".join(f"- {x}" for x in facts) + "\n</family_knowledge>"
            try:
                METRICS.inc(
                    "fuguoai_family_rag_injections_total",
                    labels={"device_id": str(device_id or "unknown")},
                )
            except Exception:
                pass
            return block
        except Exception as e:
            logger.bind(tag=TAG).warning(f"家庭知识渲染失败(降级空): {e}")
            return ""

    # ---- 抽取（三段式后台） ----
    def _extract_llm(self, config):
        if self._test_llm is not None:
            return self._test_llm
        block = self.extract_llm_block
        with self._llm_lock:
            inst = self._llm_cache.get(block)
            if inst is None:
                llm_cfg = (config.get("LLM") or {}).get(block)
                if not llm_cfg:
                    logger.bind(tag=TAG).warning(f"抽取 LLM 配置块缺失: {block}")
                    return None
                llm_type = llm_cfg.get("type", "openai")
                try:
                    inst = create_instance(llm_type, llm_cfg)
                    self._llm_cache[block] = inst
                except Exception as e:
                    logger.bind(tag=TAG).error(f"抽取 LLM 实例化失败: {e}")
                    return None
            return inst

    def extract_and_store(self, device_id, query, reply, config):
        """同步抽取并入库（在线程池内调用）。返回新增条数。绝不抛异常。"""
        if not self.enabled or not self.extract_enabled:
            return 0
        if not query or not reply:
            return 0
        llm = self._extract_llm(config)
        if llm is None:
            return 0
        prompt = _EXTRACT_PROMPT.format(user=query, assistant=reply)
        try:
            parts = []
            for chunk in llm.response_no_stream(
                system_prompt="你是家庭信息抽取器，只输出 JSON 数组。",
                user_prompt=prompt,
            ):
                parts.append(chunk)
            raw = "".join(parts)
        except Exception as e:
            logger.bind(tag=TAG).warning(f"抽取 LLM 调用失败(降级跳过): {e}")
            METRICS.inc(
                "fuguoai_family_rag_extract_errors_total",
                labels={"device_id": str(device_id or "")},
            )
            return 0
        facts = _parse_facts(raw)
        if not facts:
            return 0
        n = 0
        for fa in facts:
            if self.add_fact(device_id, fa["content"], fa["category"], fa.get("tags")):
                n += 1
        return n

    async def extract_async(self, device_id, query, reply, config):
        """后台异步抽取：超时熔断三段式，绝不阻塞主对话。"""
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(
                    self._executor,
                    lambda: self.extract_and_store(device_id, query, reply, config),
                ),
                timeout=self.extract_timeout,
            )
        except asyncio.TimeoutError:
            logger.bind(tag=TAG).warning(f"抽取超时(>{self.extract_timeout}s, 降级跳过)")
            METRICS.inc(
                "fuguoai_family_rag_extract_errors_total",
                labels={"device_id": str(device_id or "")},
            )
            return 0
        except Exception as e:
            logger.bind(tag=TAG).warning(f"抽取异常(降级跳过): {e}")
            return 0


_instance = None
_instance_lock = threading.Lock()


def _global_config():
    """从全局配置单例取配置（懒导入，避免顶层循环依赖）。

    服务启动时 server 已通过 config_holder.set_config 注入初始配置；
    此处回退用于在单例尚未初始化、且调用方未传 config 时仍能构造实例。
    """
    try:
        from fuguoai_core.utils.config_holder import get_config
        return get_config() or {}
    except Exception:
        return {}


def get_family_rag(config=None):
    """进程级单例；支持运行时热更新 enabled 开关。

    兼容两种用法：
    - 服务启动 `get_family_rag(self.config)`：首次初始化单例（config 必填语义）；
    - 路由处理器 `get_family_rag()`（无参）：直接返回已初始化的单例，
      未传 config 时【不】热更新开关，避免把单例 enabled 误改回 False。
    无参且单例尚未初始化时，从全局配置单例（config_holder）回退，保证可用。
    """
    global _instance
    with _instance_lock:
        if _instance is None:
            cfg = config if isinstance(config, dict) else _global_config()
            _instance = FamilyRAG(cfg)
        elif config is not None:
            # 仅显式传入 config 时才热更新开关
            fr_cfg = (config.get("FamilyRAG", {}) or {}) if isinstance(config, dict) else {}
            _instance.enabled = bool(fr_cfg.get("enabled", False))
            ext = (fr_cfg.get("extract") or {}) or {}
            _instance.extract_enabled = bool(ext.get("enabled", True))
        return _instance
