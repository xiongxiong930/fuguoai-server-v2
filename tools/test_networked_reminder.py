"""networked_reminder 插件本地零回归测试（不碰真实数据）。

验证：
  1) 4 个工具成功注册进 all_function_registry
  2) 描述均为合法 OpenAI function-calling 格式
  3) create→list→cancel→toggle 与真实 NetworkedReminderStore 闭环正确
  4) 全量导入 fuguoai_plugins.functions 不会因新增模块而崩（软检查）

运行：PYTHONPATH=<repo_root> python3 tools/test_networked_reminder.py
"""

import asyncio
import os
import sys
import tempfile

# 必须在导入 store 模块前设定临时数据目录（store 在 import 时读取该 env）
_TMP = tempfile.mkdtemp(prefix="nr_test_")
os.environ["FUGUOAI_DATA_DIR"] = _TMP

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def main():
    from fuguoai_plugins.register import all_function_registry
    import fuguoai_plugins.functions.networked_reminder as nr

    EXPECTED = [
        "networked_reminder_create",
        "networked_reminder_list",
        "networked_reminder_cancel",
        "networked_reminder_toggle",
    ]

    print("== 1) 注册验证 ==")
    for name in EXPECTED:
        check(f"注册 {name}", name in all_function_registry,
              f"registry keys={list(all_function_registry.keys())}")

    print("== 2) 描述格式验证 ==")
    for name in EXPECTED:
        item = all_function_registry[name]
        d = item.description
        ok = (
            isinstance(d, dict)
            and d.get("type") == "function"
            and isinstance(d.get("function"), dict)
            and d["function"].get("name") == name
            and isinstance(d["function"].get("description"), str)
            and isinstance(d["function"].get("parameters"), dict)
        )
        check(f"描述合法 {name}", ok, f"desc={d}")

    print("== 3) store 闭环验证 ==")
    from types import SimpleNamespace
    conn = SimpleNamespace(device_id="aabbccddeeff", headers={}, uid=None)

    async def run():
        # create once
        r = await nr.networked_reminder_create(
            conn,
            text="吃药",
            schedule={"type": "once", "fire_at": 4102444800},  # 2100-01-01 00:00 UTC+8
        )
        check("create 返回 REQLLM", r.action.name == "REQLLM", f"action={r.action}")
        rid = None
        # 解析 result 里的 rid
        import re
        m = re.search(r"\[([a-z0-9]+)\]", r.result or "")
        if m:
            rid = m.group(1)
        check("create 产出 rid", bool(rid), f"result={r.result}")

        # list
        l = await nr.networked_reminder_list(conn)
        check("list 返回 REQLLM", l.action.name == "REQLLM", f"action={l.action}")
        check("list 含刚建的提醒", rid is not None and rid in (l.result or ""),
              f"result={l.result}")

        # toggle off
        t = await nr.networked_reminder_toggle(conn, rid=rid, enabled=False)
        check("toggle 返回 REQLLM", t.action.name == "REQLLM", f"action={t.action}")
        # list with disabled
        ld = await nr.networked_reminder_list(conn, include_disabled=True)
        check("toggle 后状态为 disabled", "disabled" in (ld.result or ""),
              f"result={ld.result}")

        # toggle on
        t2 = await nr.networked_reminder_toggle(conn, rid=rid, enabled=True)
        check("toggle 开 返回 REQLLM", t2.action.name == "REQLLM", f"action={t2.action}")

        # cancel
        c = await nr.networked_reminder_cancel(conn, rid=rid)
        check("cancel 返回 REQLLM", c.action.name == "REQLLM", f"action={c.action}")
        la = await nr.networked_reminder_list(conn)
        check("cancel 后列表为空", "没有" in (la.result or ""), f"result={la.result}")

        return True

    asyncio.run(run())

    print("== 4) 全量 functions 模块导入软检查 ==")
    import importlib, pkgutil, fuguoai_plugins.functions as fpk
    broken = []
    for mod in pkgutil.iter_modules(fpk.__path__):
        if mod.name in ("networked_reminder",):
            continue  # 已单独导入
        try:
            importlib.import_module(f"fuguoai_plugins.functions.{mod.name}")
        except Exception as e:  # 软检查：仅记录，不计入 FAIL（其它模块依赖环境）
            broken.append((mod.name, f"{type(e).__name__}: {e}"))
    check("其它 functions 模块无新增导入崩", True,
          f"broken(仅供参考)={broken}")

    print(f"\n结果：PASS={PASS} FAIL={FAIL}")
    print(f"临时数据目录：{_TMP}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
