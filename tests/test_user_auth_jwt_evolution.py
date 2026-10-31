# -*- coding: utf-8 -*-
"""用户态/设备态 JWT 鉴权「自进化」测试（纯单元，不依赖运行服务器）。

背景：PyJWT 2.13.0 → 2.15.1（CVE 修复，commit c04dc18 + tag v1.0.1 已部署 159），
但 encode/decode Roundtrip 在 tests/ 里**零覆盖**——升级到底改了什么行为，
没有任何测试锚。本文件补上：

  A. user_auth（events WS 与全部用户态 API 的鉴权落点，api_guard → 本模块）
     - issue_token → verify_token Roundtrip
     - 空/垃圾/过期/错密钥/篡改 → None（且绝不抛）
     - 算法限制：HS512、手工 "none" alg 攻击 token → None
     - verify_mini_program_user：Bearer 头 / ?token= 查询参数兜底（WS 握手
       不转发自定义请求头，故两者共用同一校验）/ 无凭证 → None
  B. AuthToken（设备侧双层 token：外层 JWT 签名 + 内层 AES-GCM 加密 payload）
     - Roundtrip / 过期 / 篡改 / 错密钥 / 垃圾串 → (False, None)
"""

import base64
import json
import time
from datetime import datetime, timedelta, timezone

import jwt
import pytest


# ───────────────────────── A. user_auth ─────────────────────────

_TEST_SECRET = "test-jwt-secret-for-evolution"


@pytest.fixture
def ua(monkeypatch):
    """打桩 user_auth 的密钥缓存（避免读到真实 config / 落到随机密钥）。"""
    from fuguoai_core.utils import user_auth
    monkeypatch.setattr(user_auth, "_SECRET_CACHE", {"v": _TEST_SECRET})
    return user_auth


def test_issue_and_verify_roundtrip(ua):
    token = ua.issue_token({"user_id": "u-1", "openid": "o-1"})
    payload = ua.verify_token(token)
    assert payload is not None
    assert payload["user_id"] == "u-1"
    assert payload["openid"] == "o-1"
    assert payload["exp"] > time.time()


@pytest.mark.parametrize("bad", ["", None, "garbage", "a.b.c"])
def test_verify_token_rejects_bad_input(ua, bad):
    assert ua.verify_token(bad) is None


def test_verify_token_expired(ua):
    token = jwt.encode(
        {"user_id": "u-1", "openid": "o-1", "exp": int(time.time()) - 10},
        _TEST_SECRET, algorithm="HS256")
    assert ua.verify_token(token) is None


def test_verify_token_wrong_secret(ua):
    token = jwt.encode(
        {"user_id": "u-1", "openid": "o-1", "exp": int(time.time()) + 600},
        "another-secret", algorithm="HS256")
    assert ua.verify_token(token) is None


def test_verify_token_tampered_payload(ua):
    """签名不变、偷换中段 payload（改 user_id）→ 签名校验必须失败。"""
    token = ua.issue_token({"user_id": "u-1", "openid": "o-1"})
    head, _, sig = token.split(".")
    forged_body = base64.urlsafe_b64encode(json.dumps(
        {"user_id": "attacker", "openid": "o-1", "exp": int(time.time()) + 600}
    ).encode()).rstrip(b"=").decode()
    assert ua.verify_token(f"{head}.{forged_body}.{sig}") is None


def test_verify_token_rejects_other_algorithm(ua):
    """算法混淆：用 HS512 + 正确密钥签也不行（decode 限死 HS256）。"""
    token = jwt.encode(
        {"user_id": "u-1", "openid": "o-1", "exp": int(time.time()) + 600},
        _TEST_SECRET, algorithm="HS512")
    assert ua.verify_token(token) is None


def test_verify_token_rejects_none_algorithm(ua):
    """经典 alg=none 攻击 token → 拒绝。"""
    def _b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    token = (f"{_b64({'alg': 'none', 'typ': 'JWT'})}."
             f"{_b64({'user_id': 'attacker', 'openid': 'o-1', 'exp': int(time.time()) + 600})}.")
    assert ua.verify_token(token) is None


class _Req:
    def __init__(self, headers=None, query=None):
        self.headers = headers or {}
        self.query = query or {}


def test_verify_mini_program_user_bearer_header(ua):
    token = ua.issue_token({"user_id": "u-1", "openid": "o-1"})
    user = ua.verify_mini_program_user(_Req(headers={"Authorization": f"Bearer {token}"}))
    assert user is not None and user["user_id"] == "u-1"


def test_verify_mini_program_user_query_fallback(ua):
    """微信 wx.connectSocket 升级阶段不转发自定义头 → ?token= 兜底（同一校验）。"""
    token = ua.issue_token({"user_id": "u-2", "openid": "o-2"})
    user = ua.verify_mini_program_user(_Req(query={"token": token}))
    assert user is not None and user["user_id"] == "u-2"


def test_verify_mini_program_user_no_credential(ua):
    assert ua.verify_mini_program_user(_Req()) is None


def test_verify_mini_program_user_header_wins_over_query(ua):
    """两种来源同时存在时 Bearer 优先（query 兜底仅在无头时生效）。"""
    good = ua.issue_token({"user_id": "u-1", "openid": "o-1"})
    stale = ua.issue_token({"user_id": "u-2", "openid": "o-2"})
    user = ua.verify_mini_program_user(
        _Req(headers={"Authorization": f"Bearer {good}"}, query={"token": stale}))
    assert user["user_id"] == "u-1"


def test_verify_mini_program_user_malformed_bearer(ua):
    assert ua.verify_mini_program_user(
        _Req(headers={"Authorization": "Basic dXNlcjpwYXNz"})) is None


# ───────────────────────── B. AuthToken（设备侧双层 token） ─────────────────────────

@pytest.fixture
def dev_auth():
    from fuguoai_core.utils.auth import AuthToken
    return AuthToken("device-test-secret")


def test_device_token_roundtrip(dev_auth):
    token = dev_auth.generate_token("dev-1")
    ok, did = dev_auth.verify_token(token)
    assert ok is True and did == "dev-1"


def test_device_token_expired(dev_auth):
    """内层 exp 过期（外层无 exp，靠内层双检）→ 拒绝。"""
    inner = {"device_id": "dev-1",
             "exp": (datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()}
    token = jwt.encode({"data": dev_auth._encrypt_payload(inner)},
                       dev_auth.secret_key, algorithm="HS256")
    ok, did = dev_auth.verify_token(token)
    assert ok is False and did is None


def test_device_token_wrong_secret(dev_auth):
    from fuguoai_core.utils.auth import AuthToken
    other = AuthToken("another-device-secret")
    ok, did = dev_auth.verify_token(other.generate_token("dev-1"))
    assert ok is False and did is None


def test_device_token_tampered(dev_auth):
    """外层签名被换（中段 data 换成别家的加密串）→ 拒绝。"""
    from fuguoai_core.utils.auth import AuthToken
    other = AuthToken("another-device-secret")
    token = dev_auth.generate_token("dev-1")
    head, _, sig = token.split(".")
    forged = jwt.encode({"data": other._encrypt_payload(
        {"device_id": "dev-9", "exp": time.time() + 600})},
        other.secret_key, algorithm="HS256")
    ok, did = dev_auth.verify_token(f"{head}.{forged}.{sig}")
    assert ok is False and did is None


def test_device_token_garbage(dev_auth):
    ok, did = dev_auth.verify_token("not.a.jwt")
    assert ok is False and did is None
