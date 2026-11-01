"""复果品牌知识库（BrandKB）。

轻量、离线、零外部依赖的品牌/产品/门店知识库，与 TourismKB(文旅) / FamilyRAG(家庭) 解耦：

- 单分区（复果品牌域，无地域 / 跨域概念），**不套用文旅问答 persona**，不做跨域过滤；
- 存储单元 doc：``{id,title,text,category,tags,vec,emb_type,ts,weight}``；
  ``category=core`` 的条目每轮注入 ``<brand_knowledge>`` 块（第一档：保底常驻），
  全部条目可被 ``search_brand_kb`` 工具按需检索（第二档：长尾按需查）；
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
    def add_doc(self, title, text, category="longtail", tags=None):
        """录入/覆盖一条品牌知识。相同 (title,text) 视为同一条（sha1 同 id）覆盖更新。"""
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
        doc = {
            "id": did, "title": title, "text": text, "category": cat,
            "tags": tags[:8], "vec": vec, "emb_type": self._emb_type,
            "ts": time.time(), "weight": 1.0,
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

    def retrieve(self, query, top_k=None):
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
    def render_core(self):
        if not self.enabled:
            return ""
        data = self._get_mem()
        docs = [d for d in data.get("docs", []) if d.get("category") == "core"]
        if not docs:
            return ""
        blocks = [
            f"- {d.get('title', '')}：{d.get('text', '')}"
            for d in sorted(docs, key=lambda x: x.get("ts", 0))
        ]
        if not blocks:
            return ""
        return "【复果品牌知识（核心）】\n" + "\n".join(blocks)

    # ---- 管理 API ----
    def list_docs(self):
        data = self._get_mem()
        return [
            {
                "id": d.get("id"), "title": d.get("title"), "text": d.get("text"),
                "category": d.get("category"), "tags": d.get("tags"), "ts": d.get("ts"),
            }
            for d in data.get("docs", [])
        ]


_brand_kb = None
_brand_kb_lock = threading.Lock()


def get_brand_kb(config=None):
    global _brand_kb
    if _brand_kb is None:
        with _brand_kb_lock:
            if _brand_kb is None:
                _brand_kb = BrandKB(config or {})
    return _brand_kb
