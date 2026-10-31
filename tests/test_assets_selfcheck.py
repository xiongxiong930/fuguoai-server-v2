# -*- coding: utf-8 -*-
"""资产路径单一真相源 + 启动自检 的离线测试（自进化维度：防 B1 类静默失效）。

直接脚本式运行，无需 pytest。PYTHONPATH=REPO 由回归门禁/run_tests.sh 注入。
"""
import os
import shutil
import tempfile

from fuguoai_config.paths import ASSETS_DIR, CRITICAL_ASSETS, verify_critical_assets


def _check(name, cond):
    if cond:
        print(f"  [PASS] {name}")
    else:
        print(f"  [FAIL] {name}")
        raise AssertionError(name)


def test_assets_dir_resolves_to_package_location():
    # ASSETS_DIR 必须锚定到 fuguoai_config/assets，且与进程 CWD 无关
    _check(
        "ASSETS_DIR 锚定到 fuguoai_config/assets",
        ASSETS_DIR.endswith(os.path.join("fuguoai_config", "assets")),
    )
    # 在本仓内运行时，真实资产目录应存在且关键文件齐全
    if os.path.isdir(ASSETS_DIR):
        _check(
            "仓内关键音频资产齐全",
            all(os.path.exists(os.path.join(ASSETS_DIR, a)) for a in CRITICAL_ASSETS),
        )


def test_verify_raises_when_critical_missing():
    tmp = tempfile.mkdtemp()
    try:
        d = os.path.join(tmp, "assets")
        os.makedirs(d)
        # 只放一个关键文件，其余缺失
        open(os.path.join(d, "bind_code.wav"), "w").close()
        try:
            verify_critical_assets(d)
        except FileNotFoundError:
            return
        raise AssertionError("缺失关键资产时应当抛出 FileNotFoundError")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_verify_passes_when_all_present():
    tmp = tempfile.mkdtemp()
    try:
        d = os.path.join(tmp, "assets")
        os.makedirs(d)
        for a in CRITICAL_ASSETS:
            p = os.path.join(d, a)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").close()
        _check("全部齐全时自检通过", verify_critical_assets(d) is True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_verify_skips_when_dir_absent():
    d = os.path.join(tempfile.mkdtemp(), "nope")
    _check("资产目录缺失时跳过(返回 False 不抛错)", verify_critical_assets(d) is False)


if __name__ == "__main__":
    test_assets_dir_resolves_to_package_location()
    test_verify_raises_when_critical_missing()
    test_verify_passes_when_all_present()
    test_verify_skips_when_dir_absent()
    print("\nALL PASS ✅")
