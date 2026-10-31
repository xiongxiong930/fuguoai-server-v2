"""全模块导入冒烟：部署前必跑。

逐个 import 全仓所有 .py 模块（跳过脚本式测试与 __main__ 守卫文件），
捕获 ImportError / SyntaxError 之外的所有异常，输出失败清单。

用法：
  PYTHONPATH=<仓根> python3 import_smoke.py
退出码 0 = 全部可导入；1 = 有模块导入失败。
"""
import importlib
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
PACKAGES = ("fuguoai_core", "fuguoai_config", "fuguoai_plugins")
SKIP_DIRS = {"__pycache__", "tests"}

failed = []
ok = 0
skipped = []

for pkg in PACKAGES:
    base = os.path.join(ROOT, pkg)
    if not os.path.isdir(base):
        continue
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in sorted(filenames):
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, ROOT)
            modname = rel[:-3].replace(os.sep, ".")
            if modname.endswith(".__init__"):
                modname = modname[: -len(".__init__")]
            try:
                importlib.import_module(modname)
                ok += 1
            except Exception as exc:
                failed.append((modname, f"{type(exc).__name__}: {exc}"))

print(f"可导入模块：{ok}")
print(f"导入失败：{len(failed)}")
if failed:
    print("\n── 失败清单 ──")
    for mod, err in failed:
        print(f"  {mod}\n      {err}")
    print("\n── 首个失败的完整栈 ──")
    try:
        importlib.import_module(failed[0][0])
    except Exception:
        traceback.print_exc()

sys.exit(1 if failed else 0)
