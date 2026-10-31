"""T19：M8 阶段1 设备隔离门禁逻辑测试（纯后端，无需固件 / 无需 WS 基础设施）。

验证点：
- allow_all 策略：任何设备都放行（仅标注状态）
- require_activation：已激活设备放行，未知设备拒绝
- require_activation + 表缺失：降级为放行（绝不阻断，保证 M11 落表前安全）
- set_status 写入后缓存即时生效
"""
import os
import sys
import tempfile

sys.path.insert(0, "/opt/fuguoai-server")

from fuguoai_core.device_access import (
    DeviceAccess,
    DeviceBindingStore,
    POLICY_ALLOW_ALL,
    POLICY_REQUIRE_ACTIVATION,
)


def _make_da(policy, store=None):
    cfg = {"server": {"device_isolation": {"enabled": True, "policy": policy}}}
    da = DeviceAccess(cfg)
    if store is not None:
        da.store = store
    return da


def test_allow_all_always_allowed():
    da = _make_da(POLICY_ALLOW_ALL)
    allowed, _, status = da.check("ANY-DEVICE-123")
    assert allowed is True, "allow_all 必须始终放行"
    assert status in ("active", "guest")


def test_require_activation_unknown_denied():
    tmp = tempfile.mktemp(suffix=".db")
    store = DeviceBindingStore(db_path=tmp)
    store.set_status("DEV-ACTIVE", "active")
    da = _make_da(POLICY_REQUIRE_ACTIVATION, store=store)

    allowed_a, _, status_a = da.check("DEV-ACTIVE")
    assert allowed_a is True and status_a == "active", "已激活设备必须放行"

    allowed_x, reason, status_x = da.check("DEV-UNKNOWN")
    assert allowed_x is False and status_x == "inactive", "未激活设备必须拒绝"
    assert reason == "device not activated"
    os.remove(tmp)


def test_require_activation_missing_table_graceful():
    tmp = tempfile.mktemp(suffix=".db")
    if os.path.exists(tmp):
        os.remove(tmp)
    # 表尚未创建（M11 未落库前），必须降级为放行，绝不阻断
    store = DeviceBindingStore(db_path=tmp)
    da = _make_da(POLICY_REQUIRE_ACTIVATION, store=store)
    allowed, _, _ = da.check("DEV-X")
    assert allowed is True, "表缺失时必须降级放行"
    if os.path.exists(tmp):
        os.remove(tmp)


def test_disabled_is_allow_all():
    # enabled=false 时即便 policy=require_activation 也应放行
    cfg = {
        "server": {
            "device_isolation": {"enabled": False, "policy": POLICY_REQUIRE_ACTIVATION}
        }
    }
    da = DeviceAccess(cfg)
    allowed, _, status = da.check("DEV-ANY")
    assert allowed is True


if __name__ == "__main__":
    test_allow_all_always_allowed()
    test_require_activation_unknown_denied()
    test_require_activation_missing_table_graceful()
    test_disabled_is_allow_all()
    print("T19 device_access: ALL PASSED")
