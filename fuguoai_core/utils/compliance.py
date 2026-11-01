"""复果合规层（Compliance Layer，#146 示范轴）。

提供三类合规原语，供全栈按需调用：

1. **合规开关（ComplianceConfig）**：儿童模式 / 录音不出域 / 物理静音配合 / 加密总开关，
   从配置段 ``Compliance`` 读取，支持运行时热更新。当前为**全局开关**；``tenant_id`` 维度已
   预留（审计日志与决策函数均接受 ``tenant_id``），但 device→tenant 映射未接，多租户分权为
   后续接入点（与 #145 同范式：先建维度，映射后接）。

2. **审计日志（AuditStore）**：SQLite 落库（``data/compliance.db`` 的 ``audit_log`` 表），
   append-only，记录合规相关事件（内容审核、录音访问、数据导出、配置变更等）；支持按
   事件类型 / 主体 / 租户 / 时间窗查询。审计本身**始终开启**（它是证据链），不随
   ``enabled`` 开关关闭——``enabled`` 只门控"开关是否生效"，不门控审计落库。

3. **数据加密标记（FieldCrypt + 标记约定）**：敏感字段落库时可用 AES-256-GCM 加密并打
   ``enc`` 标记，或仅打 ``encrypted_at_rest`` 标记（无密钥场景，如外部 KMS 已加密）；
   二者均提供 ``is_encrypted`` 判别，满足「数据加密标记可审计」要求。密钥取自配置
   ``Compliance.secret`` 或环境变量 ``FUGUOAI_COMPLIANCE_SECRET``，绝不硬编码。

设计约定（与 BrandKB / TourismKB 同范式）：
- 进程级单例 ``get_compliance`` / ``get_audit_store``，支持热更新；
- 路由不落库逻辑，落库走本模块存储层；
- 异常 fail-soft：合规层自身异常不得拖垮主对话链路（``record_audit`` 已内部吞异常）。
"""

import base64
import json
import os
import sqlite3
import threading
import time
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

from fuguoai_config.config_loader import get_project_dir
from fuguoai_config.logger import setup_logging

TAG = __name__
logger = setup_logging()

# 多租户隔离预留（#145 同范式）：默认单租户；既有审计记录无 tenant_id 字段按 default 处理
_DEFAULT_TENANT = "default"

# FieldCrypt 参数（独立派生，不沿用 auth.py 设备 token 硬契约的 salt/iter）
_KDF_SALT_LEN = 16
_KDF_IV_LEN = 12
_KDF_ITER = 100_000


# ----------------------------------------------------------------------
# 合规开关（ComplianceConfig）
# ----------------------------------------------------------------------
class ComplianceConfig:
    """合规开关集合，从配置段 ``Compliance`` 实例化；支持运行时热更新。"""

    def __init__(self, config):
        cfg = (config.get("Compliance", {}) or {}) if isinstance(config, dict) else {}
        self._raw = cfg
        self.enabled = bool(cfg.get("enabled", False))
        self.child_mode_enabled = bool(cfg.get("child_mode_enabled", False))
        self.recording_no_egress = bool(cfg.get("recording_no_egress", False))
        self.physical_mute_enforced = bool(cfg.get("physical_mute_enforced", False))
        self.encrypt_at_rest = bool(cfg.get("encrypt_at_rest", False))
        self.secret = (cfg.get("secret") or os.environ.get("FUGUOAI_COMPLIANCE_SECRET") or "")

    # ---- 运行时热更新（与 BrandKB 同范式，reload / 每连接同步时调用）----
    def hot_update(self, config):
        cfg = (config.get("Compliance", {}) or {}) if isinstance(config, dict) else {}
        self.enabled = bool(cfg.get("enabled", False))
        self.child_mode_enabled = bool(cfg.get("child_mode_enabled", False))
        self.recording_no_egress = bool(cfg.get("recording_no_egress", False))
        self.physical_mute_enforced = bool(cfg.get("physical_mute_enforced", False))
        self.encrypt_at_rest = bool(cfg.get("encrypt_at_rest", False))
        secret = (cfg.get("secret") or os.environ.get("FUGUOAI_COMPLIANCE_SECRET") or "")
        self.secret = secret

    def snapshot(self):
        """对外暴露的开关快照（不含 secret 明文）。"""
        return {
            "enabled": self.enabled,
            "child_mode_enabled": self.child_mode_enabled,
            "recording_no_egress": self.recording_no_egress,
            "physical_mute_enforced": self.physical_mute_enforced,
            "encrypt_at_rest": self.encrypt_at_rest,
            "has_secret": bool(self.secret),
        }

    # ---- 决策函数（tenant_id 预留，当前全局生效）----
    def should_apply_child_mode(self, tenant_id=None):
        """是否启用儿童模式（内容安全过滤）。"""
        return self.enabled and self.child_mode_enabled

    def is_recording_egress_blocked(self, tenant_id=None):
        """录音是否禁止出域（本地留存，任何外发接口须拦截）。"""
        return self.enabled and self.recording_no_egress

    def is_physical_mute_enforced(self, tenant_id=None):
        """是否强制物理静音配合（合规要求硬件静音键可用）。"""
        return self.enabled and self.physical_mute_enforced

    def is_encrypt_at_rest(self, tenant_id=None):
        """是否要求敏感数据落库加密。"""
        return self.enabled and self.encrypt_at_rest


# ----------------------------------------------------------------------
# 审计日志（AuditStore）—— SQLite append-only
# ----------------------------------------------------------------------
class AuditStore:
    """合规审计日志，落库 ``data/compliance.db`` 的 ``audit_log`` 表。

    始终可用、异常不向上传播（调用方无需 try）；落库失败只在日志告警。
    """

    def __init__(self, db_path=None):
        self.db_path = db_path or os.path.join(get_project_dir(), "data", "compliance.db")
        self._lock = threading.RLock()
        try:
            self._init_schema()
        except Exception as e:
            logger.bind(tag=TAG).warning(f"合规审计库建表失败(降级空): {e}")

    def _connect(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        return sqlite3.connect(self.db_path, timeout=5.0)

    def _init_schema(self):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS audit_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts REAL NOT NULL,
                        event_type TEXT NOT NULL,
                        category TEXT NOT NULL DEFAULT 'general',
                        actor TEXT,
                        tenant_id TEXT NOT NULL DEFAULT 'default',
                        result TEXT NOT NULL DEFAULT 'info',
                        detail TEXT
                    )"""
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_log(event_type)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_tenant ON audit_log(tenant_id)")
                conn.commit()
            finally:
                conn.close()

    def log(self, event_type, actor=None, detail=None, result="info",
            tenant_id=_DEFAULT_TENANT, category="general"):
        """追加一条审计记录；返回行 id，失败返回 None。"""
        if not event_type:
            return None
        detail_json = json.dumps(detail, ensure_ascii=False) if detail is not None else None
        try:
            with self._lock:
                conn = self._connect()
                try:
                    cur = conn.execute(
                        "INSERT INTO audit_log "
                        "(ts, event_type, category, actor, tenant_id, result, detail) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (time.time(), event_type, category,
                         actor, tenant_id or _DEFAULT_TENANT, result, detail_json),
                    )
                    conn.commit()
                    return cur.lastrowid
                finally:
                    conn.close()
        except Exception as e:
            logger.bind(tag=TAG).warning(f"审计落库失败(忽略): {e}")
            return None

    def query(self, event_type=None, actor=None, tenant_id=None, since=None, limit=200):
        """按过滤条件倒序查询；返回 dict 列表（detail 反序列化）。"""
        clauses = []
        params = []
        if event_type:
            clauses.append("event_type=?")
            params.append(event_type)
        if actor:
            clauses.append("actor=?")
            params.append(actor)
        if tenant_id is not None:
            clauses.append("tenant_id=?")
            params.append(tenant_id)
        if since is not None:
            clauses.append("ts>=?")
            params.append(float(since))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(int(limit))
        try:
            with self._lock:
                conn = self._connect()
                try:
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        "SELECT id, ts, event_type, category, actor, tenant_id, result, detail "
                        f"FROM audit_log {where} ORDER BY ts DESC LIMIT ?",
                        params,
                    ).fetchall()
                    out = []
                    for r in rows:
                        d = dict(r)
                        if d.get("detail"):
                            try:
                                d["detail"] = json.loads(d["detail"])
                            except Exception:
                                pass
                        out.append(d)
                    return out
                finally:
                    conn.close()
        except Exception as e:
            logger.bind(tag=TAG).warning(f"审计查询失败(空): {e}")
            return []

    def count(self, event_type=None, tenant_id=None):
        """统计条数。"""
        clauses = []
        params = []
        if event_type:
            clauses.append("event_type=?")
            params.append(event_type)
        if tenant_id is not None:
            clauses.append("tenant_id=?")
            params.append(tenant_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        try:
            with self._lock:
                conn = self._connect()
                try:
                    row = conn.execute(
                        f"SELECT COUNT(*) FROM audit_log {where}", params
                    ).fetchone()
                    return row[0] if row else 0
                finally:
                    conn.close()
        except Exception:
            return 0


# ----------------------------------------------------------------------
# 数据加密标记（FieldCrypt + 标记约定）
# ----------------------------------------------------------------------
def _derive_key(secret: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=_KDF_ITER)
    return kdf.derive(secret.encode("utf-8"))


def encrypt_field(plaintext: str, secret: str) -> dict:
    """用 AES-256-GCM 加密单个字段，返回自包含信封（含 enc 标记 + salt/iv/ct）。

    信封结构：``{"enc": True, "v": 1, "salt": b64, "iv": b64, "ct": b64}``。
    ``secret`` 为空抛 ``ValueError``（绝不硬编码密钥）。
    """
    if not secret:
        raise ValueError("compliance secret required for field crypto")
    salt = os.urandom(_KDF_SALT_LEN)
    key = _derive_key(secret, salt)
    iv = os.urandom(_KDF_IV_LEN)
    ct = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), None)
    return {
        "enc": True,
        "v": 1,
        "salt": base64.b64encode(salt).decode("ascii"),
        "iv": base64.b64encode(iv).decode("ascii"),
        "ct": base64.b64encode(ct).decode("ascii"),
    }


def decrypt_field(blob, secret: str) -> str:
    """解密 ``encrypt_field`` 产出的信封。非信封结构抛 ``ValueError``。"""
    if not isinstance(blob, dict) or not blob.get("enc"):
        raise ValueError("not an encrypted envelope")
    if not secret:
        raise ValueError("compliance secret required for field crypto")
    salt = base64.b64decode(blob["salt"])
    iv = base64.b64decode(blob["iv"])
    ct = base64.b64decode(blob["ct"])
    key = _derive_key(secret, salt)
    pt = AESGCM(key).decrypt(iv, ct, None)
    return pt.decode("utf-8")


def mark_encrypted(meta) -> dict:
    """纯标记：无密钥场景，声明该记录的敏感字段已在别处加密（如外部 KMS）。

    返回（或就地修改）带 ``encrypted_at_rest=True`` 的 dict，绝不触碰字段内容。
    """
    if not isinstance(meta, dict):
        meta = {}
    meta["encrypted_at_rest"] = True
    return meta


def is_encrypted(meta) -> bool:
    """判别某记录是否带加密标记（enc 信封 或 encrypted_at_rest 标记）。"""
    if isinstance(meta, dict):
        return bool(meta.get("enc")) or bool(meta.get("encrypted_at_rest"))
    return False


# ----------------------------------------------------------------------
# 单例 + 便利函数
# ----------------------------------------------------------------------
_compliance = None
_compliance_lock = threading.Lock()
_audit_store = None
_audit_store_lock = threading.Lock()


def _global_config():
    """从全局配置单例取配置（懒导入，避免顶层循环依赖；与 get_brand_kb 同范式）。"""
    try:
        from fuguoai_core.utils.config_holder import get_config
        return get_config() or {}
    except Exception:
        return {}


def get_compliance(config=None):
    """进程级单例；支持运行时热更新 enabled 开关与密钥。"""
    global _compliance
    if _compliance is None:
        with _compliance_lock:
            if _compliance is None:
                cfg = config if isinstance(config, dict) else _global_config()
                _compliance = ComplianceConfig(cfg)
    elif isinstance(config, dict):
        _compliance.hot_update(config)
    return _compliance


def get_audit_store() -> AuditStore:
    """进程级审计库单例。"""
    global _audit_store
    if _audit_store is None:
        with _audit_store_lock:
            if _audit_store is None:
                _audit_store = AuditStore()
    return _audit_store


def record_audit(event_type, actor=None, detail=None, result="info",
                 tenant_id=_DEFAULT_TENANT, category="general") -> Optional[int]:
    """便利函数：写一条审计（内部吞异常，fail-soft）。"""
    try:
        return get_audit_store().log(
            event_type, actor=actor, detail=detail, result=result,
            tenant_id=tenant_id, category=category,
        )
    except Exception as e:
        logger.bind(tag=TAG).warning(f"审计写入失败(忽略): {e}")
        return None
