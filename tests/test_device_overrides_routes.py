# -*- coding: utf-8 -*-
"""设备级覆盖查看路由（离线冒烟）。

验证 device_overrides_routes：模块可导入、路由注册正确、_overlay_only 仅抽取覆盖维度。
不依赖 aiohttp 运行时（route handler 的端到端由部署后在线测试覆盖）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fuguoai_core.api import device_overrides_routes as mod


class _FakeRouter:
    def __init__(self):
        self.routes = []

    def add_get(self, path, handler):
        self.routes.append(("GET", path, handler))


class _FakeApp:
    def __init__(self):
        self.router = _FakeRouter()


def test_module_imports_and_registers():
    assert callable(mod.register)
    app = _FakeApp()
    mod.register(app)
    paths = [p for _, p, _ in app.router.routes]
    assert "/api/v1/device-overrides" in paths
    assert "/api/v1/device-overrides/{device_id}" in paths


def test_overlay_only_extracts_dimensions():
    cfg = {
        "device_id": "d1", "user_id": "u1", "prompt": "x",
        "tts_provider": "edge", "voice": "zh-CN-Xiaoxiao", "speed": 1.0,
        "volume": 1.0, "tone": "warm", "dialect": "mandarin",
        "active_preset": "p1", "wakeup_words": "小福", "updated_at": 1,
    }
    ov = mod._overlay_only(cfg)
    assert ov == {
        "tts_provider": "edge", "voice": "zh-CN-Xiaoxiao", "speed": 1.0,
        "volume": 1.0, "tone": "warm", "dialect": "mandarin", "active_preset": "p1",
    }
    assert mod._overlay_only(None) is None


if __name__ == "__main__":
    test_module_imports_and_registers()
    test_overlay_only_extracts_dimensions()
    print("test_device_overrides_routes: ALL PASS")
