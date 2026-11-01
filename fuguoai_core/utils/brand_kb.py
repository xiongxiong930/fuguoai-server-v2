"""复果品牌知识库（BrandKB）。

轻量、离线、零外部依赖的品牌/产品/门店知识库，与 TourismKB(文旅) / FamilyRAG(家庭) 解耦：

- 单分区（复果品牌域，无地域 / 跨域概念），**不套用文旅问答 persona**，不做跨域过滤；
- 存储单元 doc：``{id,title,text,category,tags,vec,emb_type,ts,weight,status,reviewer,review_note,review_ts,tenant_id}``；
  ``category=core`` 的条目每轮注入 ``<brand_knowledge>`` 块（第一档：保底常驻），
  全部条目可被 ``search_brand_kb`` 工具按需检索（第二档：长尾按需查）；
- **内容审核流（#144）**：doc 拥有 ``status`` 生命周期 ``draft → published | rejected``，
  只有 ``status=published`` 才进检索 / core 注入链路；``draft``（待审）/ ``rejected``（已拒）
  不对外生效，直到经审核 API 置为 ``published``（向后兼容：无 status 字段的旧条目按 published 处理）。
- **多租户隔离（#145，示范轴）**：doc 带 ``tenant_id``（默认 ``default`` 单租户），
  检索 / core 注入 / 管理 API 均按 ``tenant_id`` 隔离；跨租户一律不可见。
  既有条目无 ``tenant_id`` 字段 → 按 ``default`` 处理（向后兼容，现有部署零变更）。
  设备对话路径的 tenant 解析（device → tenant 映射）为后续接入点，当前默认 ``default``。
- 复用 FamilyRAG 的检索范式（可插拔 embedding / 向量自动迁移 / 余弦 top-k / 原子写 / 内存缓存 + 锁外落盘）。
"""

import copy
import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from fuguoai_config.config_loader import get_project_dir
from fuguoai_config.logger import setup_logging
from fuguoai_core.providers.embedding import create_embedding_instance

TAG = __name__
logger = setup_logging()

_CATEGORIES = {"core", "longtail", "other"}
# 内容审核流状态机：draft(待审) → published(已发布,进链路) | rejected(已拒,不进链路)
_STATUS = {"draft", "published", "rejected"}
# 多租户隔离（#145）：默认单租户；既有条目无 tenant_id 字段按 default 处理
_DEFAULT_TENANT = "default"


class BrandKB:
    def __init__(self, config):
        cfg = (config.get("BrandKB", {}) or {}) if isinstance(config, dict) else {}
        self.enabled = bool(cfg.get("enabled", False))
        self.top_k = int(cfg.get("top_k", 3))
        self.min_score = float(cfg.get("min_score", 0.0) or 0.0)
        # 相对衰减：仅保留不显著逊于 top1 的条目（top1 - rel_gap 为下限），避免噪声陪跑。
        self.rel_gap = float(cfg.get("rel_gap", 0.0) or 0.0)
        emb_cfg = (cfg.get("embedding") or {}) or {}
        emb_type = emb_cfg.get("type", "local")
        try:
            self.embedding = create_embedding_instance(emb_type, emb_cfg)
        except Exception as e:
            logger.bind(tag=TAG).warning(f"embedding 初始化失败，降级 local: {e}")
            self.embedding = create_embedding_instance("local", {})
        self._emb_type = getattr(self.embedding, "type_key", emb_type)
        self._migrate_lock = threading.Lock()
        self._migrate_ts = {}
        self._dir = os.path.join(get_project_dir(), "data", "brand_kb")
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="brandkb")
        self._mem = {}
        self._io_lock = threading.RLock()

    # ---- 存储 ----
    def _path(self):
        return os.path.join(self._dir, "brand.json")

    def _load(self):
        p = self._path()
        try:
            if not os.path.exists(p):
                return {"docs": []}
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {"docs": []}
            return self._ensure_vectors(data)
        except Exception as e:
            logger.bind(tag=TAG).warning(f"品牌知识读取失败(降级空): {e}")
            return {"docs": []}

    def _save(self, data):
        p = self._path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, p)

    def _get_mem(self):
        with self._io_lock:
            data = self._mem.get("brand")
            if data is None:
                data = self._load()
                self._mem["brand"] = data
            return data

    def _ensure_vectors(self, data):
        docs = data.get("docs", []) if isinstance(data, dict) else []
        if not docs:
            return data
        now = time.time()
        with self._migrate_lock:
            last = self._migrate_ts.get("brand", 0)
            if now - last < 60:
                return data
            self._migrate_ts["brand"] = now
        changed = False
        for d in docs:
            if d.get("emb_type") == self._emb_type:
                continue
            content = ((d.get("title") or "") + "\n" + (d.get("text") or "")).strip()
            if not content:
                continue
            try:
                vec = self.embedding.embed([content])[0]
            except Exception:
                vec = None
            if not vec or all(float(x) == 0.0 for x in vec):
                continue
            d["vec"] = vec
            d["emb_type"] = self._emb_type
            changed = True
        if changed:
            try:
                with self._io_lock:
                    snapshot = copy.deepcopy(data)
                self._save(snapshot)
            except Exception:
                pass
        return data

    # ---- 录入 ----
    def add_doc(self, title, text, category="longtail", tags=None, status="published",
                reviewer=None, review_note=None, tenant_id=_DEFAULT_TENANT):
        """录入/覆盖一条品牌知识。相同 (title,text) 视为同一条（sha1 同 id）覆盖更新。

        status: ``draft``(待审) / ``published``(已发布,默认) / ``rejected``(已拒)。
        检索与 core 注入仅取 ``published``；``draft`` / ``rejected`` 不进链路，直到经审核
        API 置为 ``published``。tenant_id 默认 ``default``（单租户）；多租户部署时按租户隔离。
        """
        if not self.enabled:
            return None
        title = (title or "").strip()
        text = (text or "").strip()
        if len(text) < 2:
            return None
        did = hashlib.sha1((title + "\x00" + text).encode("utf-8")).hexdigest()[:16]
        content = title + "\n" + text
        try:
            vec = self.embedding.embed([content])[0]
        except Exception as e:
            logger.bind(tag=TAG).warning(f"embedding 失败(跳过录入): {e}")
            return None
        if not vec or all(float(x) == 0.0 for x in vec):
            logger.bind(tag=TAG).warning("embedding 返回零向量(跳过录入)")
            return None
        cat = category if category in _CATEGORIES else "other"
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        if not isinstance(tags, list):
            tags = []
        st = status if status in _STATUS else "published"
        doc = {
            "id": did, "title": title, "text": text, "category": cat,
            "tags": tags[:8], "vec": vec, "emb_type": self._emb_type,
            "ts": time.time(), "weight": 1.0,
            "status": st, "reviewer": reviewer, "review_note": review_note,
            "review_ts": time.time() if reviewer else None,
            "tenant_id": tenant_id or _DEFAULT_TENANT,
        }
        with self._lock:
            data = self._get_mem()
            docs = data.setdefault("docs", [])
            for i, d in enumerate(docs):
                if d.get("id") == did:
                    docs[i] = doc
                    break
            else:
                docs.append(doc)
            with self._io_lock:
                snapshot = copy.deepcopy(data)
                self._save(snapshot)
                self._mem["brand"] = data
        return doc

    # ---- 检索 ----
    @staticmethod
    def _cosine(a, b):
        a = np.asarray(a, dtype=float)
        b = np.asarray(b, dtype=float)
        na = np.linalg.norm(a)
        nb = np.linalg.norm(b)
        if na == 0 or nb == 0:
            return 0.0
        return float(np.dot(a, b) / (na * nb))

    def retrieve(self, query, top_k=None, tenant_id=_DEFAULT_TENANT):
        if not self.enabled or not query:
            return []
        try:
            qv = self.embedding.embed([query])[0]
        except Exception:
            return []
        if not qv or all(float(x) == 0.0 for x in qv):
            return []
        data = self._get_mem()
        docs = data.get("docs", []) if isinstance(data, dict) else []
        if not docs:
            return []
        scored = []
        for d in docs:
            # 多租户隔离（#145）：跨租户不可见（无 tenant_id 字段的旧条目按 default 处理）
            if (d.get("tenant_id") or _DEFAULT_TENANT) != (tenant_id or _DEFAULT_TENANT):
                continue
            # 审核流：仅 published 进入检索链路（draft/rejected 不对外生效）
            if d.get("status", "published") != "published":
                continue
            v = d.get("vec")
            if not v or d.get("emb_type") != self._emb_type:
                continue
            s = self._cosine(qv, v) * float(d.get("weight", 1.0))
            scored.append((s, d))
        scored.sort(key=lambda x: x[0], reverse=True)
        if not scored:
            return []
        top_k = top_k or self.top_k
        top_score = scored[0][0]
        out = []
        for s, d in scored:
            if self.min_score and s < self.min_score:
                break
            if self.rel_gap and (top_score - s) > self.rel_gap:
                continue
            out.append(d)
            if len(out) >= top_k:
                break
        return out

    # ---- 第一档：core 块渲染（注入动态 system）----
    def render_core(self, tenant_id=_DEFAULT_TENANT):
        if not self.enabled:
            return ""
        data = self._get_mem()
        docs = [
            d for d in data.get("docs", [])
            if d.get("category") == "core"
            and d.get("status", "published") == "published"
            and (d.get("tenant_id") or _DEFAULT_TENANT) == (tenant_id or _DEFAULT_TENANT)
        ]
        if not docs:
            return ""
        blocks = [
            f"- {d.get('title', '')}：{d.get('text', '')}"
            for d in sorted(docs, key=lambda x: x.get("ts", 0))
        ]
        if not blocks:
            return ""
        return "【复果品牌知识（核心）】\n" + "\n".join(blocks)

    # ---- 管理 / 审核 API ----
    def list_docs(self, status_filter=None, tenant_id=None):
        """列出条目。tenant_id=None 表示跨租户全量（管理视图）；否则按租户过滤。"""
        data = self._get_mem()
        out = []
        for d in data.get("docs", []):
            st = d.get("status", "published")
            if status_filter and st != status_filter:
                continue
            if tenant_id is not None and (d.get("tenant_id") or _DEFAULT_TENANT) != tenant_id:
                continue
            out.append({
                "id": d.get("id"), "title": d.get("title"), "text": d.get("text"),
                "category": d.get("category"), "tags": d.get("tags"), "ts": d.get("ts"),
                "status": st, "reviewer": d.get("reviewer"),
                "review_note": d.get("review_note"), "review_ts": d.get("review_ts"),
                "tenant_id": d.get("tenant_id") or _DEFAULT_TENANT,
            })
        return out

    def get_doc(self, doc_id, tenant_id=None):
        """取单条。tenant_id=None 表示跨租户（按 id 直取）；否则限定租户。"""
        data = self._get_mem()
        for d in data.get("docs", []):
            if d.get("id") == doc_id:
                if tenant_id is not None and (d.get("tenant_id") or _DEFAULT_TENANT) != tenant_id:
                    return None
                return d
        return None

    def set_status(self, doc_id, status, reviewer=None, note=None, tenant_id=None):
        """审核动作：在 ``draft`` / ``published`` / ``rejected`` 之间切换，并记录审核人 / 意见 / 时间。

        返回更新后的 doc；``doc_id`` 不存在（或限定租户下不匹配）返回 ``None``；
        ``status`` 非法抛 ``ValueError``。tenant_id=None 表示跨租户（按 id 直取）。
        """
        if status not in _STATUS:
            raise ValueError(f"invalid status: {status}")
        with self._lock:
            data = self._get_mem()
            docs = data.setdefault("docs", [])
            for i, d in enumerate(docs):
                if d.get("id") == doc_id:
                    if tenant_id is not None and (d.get("tenant_id") or _DEFAULT_TENANT) != tenant_id:
                        return None
                    d = dict(d)
                    d["status"] = status
                    d["reviewer"] = reviewer
                    d["review_note"] = note
                    d["review_ts"] = time.time()
                    docs[i] = d
                    with self._io_lock:
                        snapshot = copy.deepcopy(data)
                        self._save(snapshot)
                        self._mem["brand"] = data
                    return d
        return None


_brand_kb = None
_brand_kb_lock = threading.Lock()


def _global_config():
    """从全局配置单例取配置（懒导入，避免顶层循环依赖；与 get_tourism_kb 同范式）。"""
    try:
        from fuguoai_core.utils.config_holder import get_config
        return get_config() or {}
    except Exception:
        return {}


def get_brand_kb(config=None):
    """进程级单例；支持运行时热更新 enabled 开关（与 get_tourism_kb 同范式）。

    调用方不传 config 时回退全局配置单例（config_holder.get_config），
    避免「首个调用者是 HTTP 路由（无 config）导致单例以 disabled 建实例」的坑——
    那样会让 core 注入 / search_brand_kb 工具在 159 上整个失效。
    """
    global _brand_kb
    if _brand_kb is None:
        with _brand_kb_lock:
            if _brand_kb is None:
                cfg = config if isinstance(config, dict) else _global_config()
                _brand_kb = BrandKB(cfg)
    elif isinstance(config, dict):
        # 热更新 enabled 开关与检索参数（reload / 每连接同步）
        bkb_cfg = (config.get("BrandKB", {}) or {})
        _brand_kb.enabled = bool(bkb_cfg.get("enabled", False))
        _brand_kb.top_k = int(bkb_cfg.get("top_k", _brand_kb.top_k))
        _brand_kb.min_score = float(bkb_cfg.get("min_score", _brand_kb.min_score) or _brand_kb.min_score)
        _brand_kb.rel_gap = float(bkb_cfg.get("rel_gap", _brand_kb.rel_gap) or _brand_kb.rel_gap)
    return _brand_kb
