# -*- coding: utf-8 -*-
"""NFC 卡登记工具「自进化」测试（纯单元，tmp 隔离，不碰线上注册表）。

覆盖 tools/provision_cards.py——card_registry.json 的**唯一程序化写入端**
（线上表绑着真实领卡关系，写错即污染生产数据，此前零覆盖）：

  ① norm_uid 与运行时 card_registry._norm_uid **逐形态对拍**——
     登记端与读卡端归一化不一致 = 同一张物理卡被判 unknown_card
  ② load_manifest：JSON list / JSON dict({"cards":[...]}) / CSV(utf-8-sig BOM，
     Excel 导出实况) 三种形态
  ③ main 写入语义：新卡 added / serial 缺省自动派生 AH-末6位 / 字段不全 skip
  ④ main 合并语义：已存在 updated；**已领卡的 owner_uid 绝不被 manifest 覆盖**
     （防误覆盖用户绑定）；existing 无 owner 时可补；status 保留原值
  ⑤ main 兜底：坏 JSON 注册表按空库处理（stderr 警告）后照常写入；原子写不留 .tmp
  ⑥ 端到端：provision 落盘 → 运行时 CardRegistry 原样读回（锁落盘格式契约）
"""

import csv
import json
import sys

import pytest

from tools.provision_cards import load_manifest, main, norm_uid


# ───────────────────────── ① 归一化对拍（登记端 == 运行端） ─────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("04aabbccdd", "04AABBCCDD"),
    ("04 aa bb cc dd", "04AABBCCDD"),
    ("04:aa:bb:cc:dd", "04AABBCCDD"),
    ("04-aa-bb-cc-dd", "04AABBCCDD"),
    ("04_aa_bb_cc_dd", "04AABBCCDD"),
    ("  04AAbbCCDD  ", "04AABBCCDD"),
    ("", ""),
    (None, ""),
])
def test_norm_uid_matches_runtime(raw, expected):
    """登记端 norm_uid 必须与运行时 _norm_uid 完全一致（逐形态对拍）。"""
    from fuguoai_core.utils.card_registry import _norm_uid

    assert norm_uid(raw) == expected
    assert norm_uid(raw) == _norm_uid(raw)


# ───────────────────────── ② manifest 三形态 ─────────────────────────

def test_load_manifest_json_list(tmp_path):
    p = tmp_path / "cards.json"
    p.write_text(json.dumps([
        {"card_uid": "AABB", "character_id": "c1", "serial": "AH-0001"},
    ]), encoding="utf-8")
    rows = load_manifest(str(p))
    assert rows == [{"card_uid": "AABB", "character_id": "c1",
                     "serial": "AH-0001", "owner_uid": None}]


def test_load_manifest_json_dict_wrapper(tmp_path):
    """{"cards": [...]} 包裹形态同样接受。"""
    p = tmp_path / "cards.json"
    p.write_text(json.dumps({"cards": [
        {"card_uid": "AABB", "character_id": "c1", "serial": "AH-0001",
         "owner_uid": "u1"},
    ]}), encoding="utf-8")
    rows = load_manifest(str(p))
    assert rows[0]["owner_uid"] == "u1"


def test_load_manifest_csv_with_bom(tmp_path):
    """Excel 导出的 CSV 带 BOM（utf-8-sig）——表头不能被 BOM 污染。"""
    p = tmp_path / "cards.csv"
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["card_uid", "character_id", "serial"])
        w.writerow(["AABB", "c1", "AH-0001"])
        w.writerow(["CCDD", "c2", ""])
    rows = load_manifest(str(p))
    assert rows[0] == {"card_uid": "AABB", "character_id": "c1",
                       "serial": "AH-0001", "owner_uid": None}
    assert rows[1]["serial"] == ""


# ───────────────────────── ③④⑤ main 写入/合并/兜底 ─────────────────────────

def _run_main(tmp_path, rows, registry_obj=None, monkeypatch=None, env=None):
    """跑一次 main()，返回 (exit_code, registry_path)。"""
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    reg = tmp_path / "card_registry.json"
    if registry_obj is not None:
        reg.write_text(json.dumps(registry_obj, ensure_ascii=False),
                       encoding="utf-8")
    argv = ["provision_cards.py", "--manifest", str(manifest),
            "--registry", str(reg)]
    old_argv = sys.argv
    sys.argv = argv
    try:
        code = main()
    finally:
        sys.argv = old_argv
    return code, reg


def test_main_adds_new_cards(tmp_path):
    code, reg = _run_main(tmp_path, [
        {"card_uid": "04 AA BB", "character_id": "c1", "serial": "AH-0001"},
        {"card_uid": "ccdd", "character_id": "c2", "serial": "AH-0002"},
    ])
    assert code == 0
    data = json.loads(reg.read_text(encoding="utf-8"))
    # UID 归一化后落盘（小写/带空格都被规范化）
    assert set(data["cards"]) == {"04AABB", "CCDD"}
    assert data["cards"]["04AABB"]["character_id"] == "c1"
    assert data["cards"]["04AABB"]["owner_uid"] is None
    assert data["cards"]["04AABB"]["claimed_at"] is None
    assert data["cards"]["04AABB"]["lineage"] == []
    assert data["cards"]["04AABB"]["status"] == "active"


def test_main_serial_defaults_to_uid_tail(tmp_path):
    code, reg = _run_main(tmp_path, [
        {"card_uid": "AABBCCDDEEFF", "character_id": "c1", "serial": ""},
    ])
    assert code == 0
    assert json.loads(reg.read_text(encoding="utf-8"))["cards"]["AABBCCDDEEFF"]["serial"] == "AH-DDEEFF"


def test_main_skips_incomplete_rows(tmp_path, capsys):
    code, reg = _run_main(tmp_path, [
        {"card_uid": "AABB", "character_id": "c1", "serial": "AH-0001"},
        {"card_uid": "CCDD", "character_id": "", "serial": "AH-0002"},  # 缺 cid
        {"card_uid": "", "character_id": "c3", "serial": "AH-0003"},    # 缺 uid
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "skipped=2" in out
    data = json.loads(reg.read_text(encoding="utf-8"))
    assert set(data["cards"]) == {"AABB"}


def test_main_empty_manifest_exit_2(tmp_path):
    manifest = tmp_path / "empty.json"
    manifest.write_text("[]", encoding="utf-8")
    reg = tmp_path / "card_registry.json"
    old_argv = sys.argv
    sys.argv = ["provision_cards.py", "--manifest", str(manifest),
                "--registry", str(reg)]
    try:
        code = main()
    finally:
        sys.argv = old_argv
    assert code == 2
    assert not reg.exists()  # 空 manifest 不落盘（不制造空库覆盖）


def test_main_merges_and_updates_fields(tmp_path):
    code, reg = _run_main(
        tmp_path,
        [{"card_uid": "AABB", "character_id": "c2", "serial": "AH-0009"}],
        registry_obj={"cards": {"AABB": {
            "character_id": "c1", "serial": "AH-0001",
            "owner_uid": None, "claimed_at": None,
            "created_at": 100, "lineage": [], "status": "active",
        }}},
    )
    assert code == 0
    card = json.loads(reg.read_text(encoding="utf-8"))["cards"]["AABB"]
    assert card["character_id"] == "c2"   # 按 manifest 更新
    assert card["serial"] == "AH-0009"
    assert card["created_at"] == 100      # 原始创建时间保留


def test_main_never_overwrites_claimed_owner(tmp_path):
    """已领卡的 owner_uid 绝不被 manifest 覆盖——防误覆盖用户绑定关系。"""
    code, reg = _run_main(
        tmp_path,
        [{"card_uid": "AABB", "character_id": "c1", "serial": "AH-0001",
          "owner_uid": "attacker"}],
        registry_obj={"cards": {"AABB": {
            "character_id": "c1", "serial": "AH-0001",
            "owner_uid": "real-owner", "claimed_at": 123,
            "created_at": 100, "lineage": ["real-owner"], "status": "active",
        }}},
    )
    assert code == 0
    card = json.loads(reg.read_text(encoding="utf-8"))["cards"]["AABB"]
    assert card["owner_uid"] == "real-owner"       # 原绑定保留
    assert card["claimed_at"] == 123               # 领取时间保留
    assert card["lineage"] == ["real-owner"]


def test_main_fills_owner_when_existing_unclaimed(tmp_path):
    """existing 无 owner 时，manifest 带的 owner 可以补上。"""
    code, reg = _run_main(
        tmp_path,
        [{"card_uid": "AABB", "character_id": "c1", "serial": "AH-0001",
          "owner_uid": "u1"}],
        registry_obj={"cards": {"AABB": {
            "character_id": "c1", "serial": "AH-0001", "owner_uid": None,
            "claimed_at": None, "created_at": 100, "lineage": [],
            "status": "registered",
        }}},
    )
    assert code == 0
    card = json.loads(reg.read_text(encoding="utf-8"))["cards"]["AABB"]
    assert card["owner_uid"] == "u1"
    assert card["status"] == "registered"  # 合并不改原 status


def test_main_corrupt_registry_treated_as_empty(tmp_path, capsys):
    """坏 JSON 注册表 → 按空库处理（stderr 警告），不炸、照常写入。"""
    reg = tmp_path / "card_registry.json"
    reg.write_text("{not json", encoding="utf-8")
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps(
        [{"card_uid": "AABB", "character_id": "c1", "serial": "AH-0001"}]),
        encoding="utf-8")
    old_argv = sys.argv
    sys.argv = ["provision_cards.py", "--manifest", str(manifest),
                "--registry", str(reg)]
    try:
        code = main()
    finally:
        sys.argv = old_argv
    err = capsys.readouterr().err
    assert code == 0
    assert "不是合法 JSON" in err
    assert json.loads(reg.read_text(encoding="utf-8"))["cards"]["AABB"]["serial"] == "AH-0001"


def test_main_atomic_write_leaves_no_tmp(tmp_path):
    code, reg = _run_main(tmp_path, [
        {"card_uid": "AABB", "character_id": "c1", "serial": "AH-0001"},
    ])
    assert code == 0
    assert reg.exists()
    assert not (tmp_path / "card_registry.json.tmp").exists()


def test_main_registry_path_from_env(tmp_path, monkeypatch):
    """缺省 --registry 时走 $FUGUOAI_DATA_DIR（与运行时同源）。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("FUGUOAI_DATA_DIR", str(data_dir))
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps(
        [{"card_uid": "AABB", "character_id": "c1", "serial": "AH-0001"}]),
        encoding="utf-8")
    old_argv = sys.argv
    sys.argv = ["provision_cards.py", "--manifest", str(manifest)]
    try:
        code = main()
    finally:
        sys.argv = old_argv
    assert code == 0
    reg = data_dir / "card_registry.json"
    assert reg.exists()
    assert json.loads(reg.read_text(encoding="utf-8"))["cards"]["AABB"]["character_id"] == "c1"


# ───────────────────────── ⑥ 端到端：落盘 → 运行时读回 ─────────────────────────

def test_provisioned_registry_readable_by_runtime(tmp_path):
    """锁落盘格式契约：provision 写出的注册表，运行时 CardRegistry 原样读回。"""
    from fuguoai_core.utils.card_registry import CardRegistry

    code, reg = _run_main(tmp_path, [
        {"card_uid": "04:AA:BB", "character_id": "chuxue-001",
         "serial": "TAG_01"},
        {"card_uid": "ccdd", "character_id": "zero-002", "serial": "TAG_02"},
    ])
    assert code == 0
    runtime = CardRegistry(path=str(reg))
    # 读卡端用另一种物理形态（小写连字符）也能命中——归一化在两端一致
    assert runtime.resolve_character("04aa-bb") == "chuxue-001"
    assert runtime.get_by_serial("TAG_02")[1]["character_id"] == "zero-002"
    assert runtime.owns("u1", "chuxue-001") is False  # 未领
