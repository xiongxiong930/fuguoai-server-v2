"""设备人格单测（维度②E2，无网络/无 LLM）。

真进化验证：三层合并（default ← config.devices ← 文件层）、渲染降级、
持久化原子写与运行时生效。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fuguoai_core.utils.persona import PersonaStore


def _store(conf=None, path=None):
    ps = PersonaStore({"persona": conf or {}})
    if path:
        ps._path = path
        ps._last_check = 0.0
        ps._file_mtime = None
    return ps


def test_disabled_renders_empty():
    ps = _store({"enabled": False, "default": {"name": "复果"}})
    assert ps.render("dev1") == ""


def test_default_layer_render():
    ps = _store({"enabled": True, "default": {"name": "复果", "style": "温暖"}})
    out = ps.render("dev1")
    assert out.startswith("<persona>") and out.endswith("</persona>")
    assert "复果" in out and "温暖" in out


def test_device_layer_overrides_default():
    ps = _store({
        "enabled": True,
        "default": {"name": "复果", "style": "温暖"},
        "devices": {"dev2": {"name": "果果", "traits": ["活泼", "爱讲笑话"]}},
    })
    p = ps.get_persona("dev2")
    assert p["name"] == "果果"          # 设备层覆盖
    assert p["style"] == "温暖"          # 默认层保留
    out = ps.render("dev2")
    assert "果果" in out and "活泼、爱讲笑话" in out


def test_file_layer_overrides_and_runtime_reload():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, ".persona.yaml")
        ps = _store({"enabled": True, "default": {"name": "复果"}}, path=path)
        assert ps.get_persona("dev3")["name"] == "复果"
        # 运行时写入文件层，立即生效（set_persona 主动失效缓存）
        ps.set_persona("dev3", {"name": "小熊", "catchphrase": "好嘞"})
        p = ps.get_persona("dev3")
        assert p["name"] == "小熊"
        out = ps.render("dev3")
        assert "小熊" in out and "好嘞" in out
        # 清除该设备画像 → 回退默认层
        ps.set_persona("dev3", {})
        assert ps.get_persona("dev3")["name"] == "复果"


def test_corrupt_file_degrades_to_default():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, ".persona.yaml")
        with open(path, "w") as f:
            f.write("]][[ not yaml :::")
        ps = _store({"enabled": True, "default": {"name": "复果"}}, path=path)
        assert ps.get_persona("dev4")["name"] == "复果"  # 不崩，降级默认


def test_empty_profile_renders_empty():
    ps = _store({"enabled": True})
    assert ps.render("dev5") == ""


def test_taboo_rendered():
    ps = _store({"enabled": True, "default": {"name": "复果", "taboo": ["成绩", "体重"]}})
    out = ps.render("dev6")
    assert "成绩、体重" in out


if __name__ == "__main__":
    test_disabled_renders_empty()
    test_default_layer_render()
    test_device_layer_overrides_default()
    test_file_layer_overrides_and_runtime_reload()
    test_corrupt_file_degrades_to_default()
    test_empty_profile_renders_empty()
    test_taboo_rendered()
    print("PERSONA_TESTS_PASS")
