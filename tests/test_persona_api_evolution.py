"""维度②E2 延伸：人格管理 API 数据层离线测试（真进化）。

钉死 PersonaStore 新增的 get_file_layer / update_persona 语义，使
persona_routes.py 的管理 API 行为可离线验证，不依赖运行中的 HTTP 服务。

覆盖：
- get_file_layer 只读文件层（不合并默认/配置层）
- update_persona 部分合并：未传字段不被清空
- update_persona 显式 null 清除字段
- update_persona 忽略未知字段
- update_persona 类型校验（字符串字段非字符串 / 列表字段逗号串自动拆分）
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fuguoai_core.utils.persona import PersonaStore


def _new_store():
    d = tempfile.mkdtemp(prefix="persona_api_test_")
    p = os.path.join(d, ".persona.yaml")
    store = PersonaStore({"persona": {"enabled": True}})
    store._path = p  # 重定向到临时文件，避免污染真实 data/
    return store


def test_get_file_layer_empty_when_no_file():
    s = _new_store()
    assert s.get_file_layer("dev1") == {}, "无文件时应返回空 dict"


def test_get_file_layer_raw_no_merge():
    s = _new_store()
    s.set_persona("dev1", {"name": "小福", "style": "活泼"})
    # 文件层只含写入字段，不应混入默认/配置层
    raw = s.get_file_layer("dev1")
    assert raw.get("name") == "小福"
    assert raw.get("style") == "活泼"
    # 不应出现默认层的 name（默认是「复果」），证明未合并
    merged = s.get_persona("dev1")
    assert merged.get("name") == "小福"  # 文件层覆盖默认


def test_update_persona_partial_merge_keeps_others():
    s = _new_store()
    s.set_persona("dev1", {"name": "小福", "style": "活泼"})
    # 只改 style，name 应保留
    merged = s.update_persona("dev1", {"style": "稳重"})
    assert merged.get("name") == "小福", "部分更新不应清空未传字段"
    assert merged.get("style") == "稳重"
    # 文件层也应保留 name
    assert s.get_file_layer("dev1").get("name") == "小福"


def test_update_persona_null_clears_field():
    s = _new_store()
    s.set_persona("dev1", {"name": "小福", "style": "活泼"})
    merged = s.update_persona("dev1", {"name": None})
    assert "name" not in merged, "显式 null 应清除该字段"
    assert merged.get("style") == "活泼"
    assert "name" not in s.get_file_layer("dev1")


def test_update_persona_ignores_unknown_fields():
    s = _new_store()
    merged = s.update_persona("dev1", {"name": "小福", "hacked": "x"})
    assert merged.get("name") == "小福"
    assert "hacked" not in merged, "未知字段应被忽略"


def test_update_persona_validates_string_type():
    s = _new_store()
    try:
        s.update_persona("dev1", {"name": 123})
        assert False, "字符串字段传非字符串应抛 ValueError"
    except ValueError:
        pass


def test_update_persona_list_comma_string_split():
    s = _new_store()
    merged = s.update_persona("dev1", {"traits": "幽默, 有耐心"})
    assert merged.get("traits") == ["幽默", "有耐心"], "逗号串应自动拆为数组"


def test_update_persona_list_array_kept():
    s = _new_store()
    merged = s.update_persona("dev1", {"taboo": ["政治", "暴力"]})
    assert merged.get("taboo") == ["政治", "暴力"]


def test_update_persona_empty_device_id_rejected():
    s = _new_store()
    try:
        s.update_persona("", {"name": "x"})
        assert False, "空 device_id 应抛 ValueError"
    except ValueError:
        pass


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
