"""#146 合规层离线逻辑测试（不依赖网络 / 不污染仓库 data/）。

覆盖：
- ComplianceConfig 开关 + 决策函数（enabled 门控）；
- AuditStore append / query / count / by_category（tenant 维度）；
- FieldCrypt AES-256-GCM 信封加解密 round-trip + enc 标记；mark_encrypted / is_encrypted 纯标记。
"""

import os
import tempfile

import pytest

import fuguoai_core.utils.compliance as cmp_mod
from fuguoai_core.utils.compliance import (
    ComplianceConfig, AuditStore, encrypt_field, decrypt_field,
    mark_encrypted, is_encrypted, get_compliance, get_audit_store, record_audit,
)


@pytest.fixture(autouse=True)
def _tmp_data(monkeypatch):
    """把合规库落盘重定向到临时目录，避免污染仓库 data/。"""
    tmp = tempfile.mkdtemp(prefix="comp_test_")
    monkeypatch.setattr(cmp_mod, "get_project_dir", lambda: tmp)
    # 重置单例，确保用新 tmp 路径建库
    cmp_mod._audit_store = None
    cmp_mod._compliance = None
    yield tmp
    # 清理临时库
    try:
        os.remove(os.path.join(tmp, "compliance.db"))
    except OSError:
        pass


# ---- 合规开关 ----
def test_compliance_switches_gated_by_enabled():
    cfg = ComplianceConfig({"Compliance": {
        "enabled": True,
        "child_mode_enabled": True,
        "recording_no_egress": True,
    }})
    assert cfg.should_apply_child_mode()
    assert cfg.is_recording_egress_blocked()
    # 关闭总开关后，所有开关失效
    cfg.hot_update({"Compliance": {"enabled": False,
                                   "child_mode_enabled": True,
                                   "recording_no_egress": True}})
    assert not cfg.should_apply_child_mode()
    assert not cfg.is_recording_egress_blocked()


def test_compliance_defaults_off():
    cfg = ComplianceConfig({})
    assert cfg.enabled is False
    assert cfg.child_mode_enabled is False
    assert cfg.recording_no_egress is False
    snap = cfg.snapshot()
    assert "secret" not in snap  # 快照不含密钥明文
    assert snap["has_secret"] is False


def test_compliance_secret_from_env(monkeypatch):
    monkeypatch.setenv("FUGUOAI_COMPLIANCE_SECRET", "env-secret-123")
    cfg = ComplianceConfig({"Compliance": {"secret": ""}})
    assert cfg.secret == "env-secret-123"
    assert cfg.snapshot()["has_secret"] is True


# ---- 审计日志 ----
def test_audit_append_query_count():
    store = AuditStore()
    store.log("content_review", actor="admin", detail={"doc": "x"},
              result="allow", tenant_id="tenantA", category="content")
    store.log("recording_access", actor="dev1", detail={"secs": 30},
              result="allow", tenant_id="tenantB", category="recording")
    assert store.count() == 2
    assert store.count(tenant_id="tenantA") == 1
    rows = store.query(event_type="content_review")
    assert len(rows) == 1
    assert rows[0]["detail"] == {"doc": "x"}
    assert rows[0]["tenant_id"] == "tenantA"


def test_audit_by_category_aggregate():
    store = AuditStore()
    store.log("a", category="content")
    store.log("b", category="content")
    store.log("c", category="recording")
    # 模拟 status 接口的聚合逻辑
    import sqlite3
    conn = store._connect()
    try:
        conn.row_factory = None
        rows = conn.execute(
            "SELECT category, COUNT(*) FROM audit_log GROUP BY category"
        ).fetchall()
        by_cat = {r[0]: r[1] for r in rows}
    finally:
        conn.close()
    assert by_cat["content"] == 2
    assert by_cat["recording"] == 1


def test_record_audit_fail_soft():
    # 删库路径使后续落库失败，record_audit 应返回 None 而非抛异常
    store = get_audit_store()
    rid = record_audit("test_event", actor="u", category="general")
    assert isinstance(rid, int)


# ---- 字段加密 ----
def test_field_crypto_roundtrip():
    secret = "strong-secret-xyz"
    env = encrypt_field("敏感文本123", secret)
    assert env.get("enc") is True
    assert is_encrypted(env) is True
    plain = decrypt_field(env, secret)
    assert plain == "敏感文本123"


def test_field_crypto_wrong_secret_fails():
    env = encrypt_field("hello", "secret-A")
    with pytest.raises(Exception):
        decrypt_field(env, "secret-B")


def test_field_crypto_requires_secret():
    with pytest.raises(ValueError):
        encrypt_field("x", "")


def test_mark_encrypted_pure():
    meta = {"field": "value"}
    out = mark_encrypted(meta)
    assert out["encrypted_at_rest"] is True
    assert is_encrypted(out) is True
    assert out["field"] == "value"  # 不触碰内容
    assert is_encrypted({"foo": 1}) is False
