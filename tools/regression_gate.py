#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 5 回归门禁（本地离线部分）— 部署前必跑，非零退出禁止部署。

三级检查（全部离线，不碰生产容器）：
  L1  全仓 py_compile          —— 语法级，任何文件编译失败即 FAIL
  L2  关键模块 import 冒烟      —— fuguoai_core 关键路径可导入（缺三密钥属正常，
                                   只认 ImportError/SyntaxError 为 FAIL）
  L3  离线 P0 测试集            —— 逐个跑仓根 test_*.py 中无网络依赖的用例

在线部分（需要 159 容器/真机，本脚本只提示不执行）：
  test_fuguoai_backend.py / test_m7.py / test_m13/m14 系列 → 部署后在容器侧跑。

用法:
  <python> tools/regression_gate.py            # 全量 L1+L2+L3
  <python> tools/regression_gate.py --quick    # 仅 L1+L2（改动纯文档/工具时用）

推荐解释器: /Users/xiongxiong/.workbuddy/binaries/python/envs/fuguoai-regress/bin/python
"""
import os
import py_compile
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# L2: 关键 import 冒烟清单（Phase5 三层覆盖的模块 + P0 音频链路入口）
IMPORT_SMOKE = [
    "fuguoai_core.utils.util",
    "fuguoai_core.providers.tts.base",
    "fuguoai_core.providers.tts.edge",
    "fuguoai_core.utils.intercom_registry",
    "fuguoai_core.utils.intercom_stats",
    "fuguoai_core.utils.intercom_aec",
    "fuguoai_core.utils.voicemail_store",
    "fuguoai_core.utils.voicemail_player",
    "fuguoai_core.utils.networked_reminder_store",
    "fuguoai_core.utils.networked_reminder_scheduler",
    "fuguoai_core.api.networked_reminder_routes",
    "fuguoai_core.api.admin_devices_routes",
    "fuguoai_core.api.weather_routes",
    "fuguoai_core.utils.proactive_engine",
    "fuguoai_core.api.proactive_routes",
    "fuguoai_plugins.functions.get_weather",
]


def _discover_route_modules():
    """把 fuguoai_core/api 下所有 *routes.py 动态纳入 L2 冒烟。

    教训(2026-08-11): events_routes.py 漏写 import 触发模块级 NameError，
    被 apply_routes 的 fail-soft 静默跳过，导致线上整条 /api/v1/events 路由 404；
    而当时 L2 冒烟清单既不含 *routes 模块、又只识别 ImportError 关键字，漏网。
    改为全量扫描路由模块 + 非零退出即 FAIL，可在部署前拦下此类导入期错误。
    """
    found = []
    for base in ("fuguoai_core/api", "fuguoai_core/api/features"):
        d = os.path.join(REPO, base)
        if not os.path.isdir(d):
            continue
        for entry in sorted(os.listdir(d)):
            if entry.endswith("_routes.py") and not entry.startswith("__"):
                found.append(f"{base.replace('/', '.')}.{entry[:-3]}")
    return found


IMPORT_SMOKE = IMPORT_SMOKE + _discover_route_modules()

# L3: 离线可跑的测试（无网络/无容器依赖）
OFFLINE_TESTS = [
    "test_tts_segment.py",
    "test_tencent_stream.py",
    "test_edge_stream.py",
    "test_edge_timeout.py",
    "test_intercom_transcode.py",
    "test_intercom_stats.py",
    "test_intercom_bootstrap_relay_metrics.py",
    "test_intercom_watchdog.py",
    "test_intercom_aec.py",
    "test_aec_auto_tune.py",
    "test_conn_storm_guard.py",
    "test_log_throttle_and_send_timeout.py",
    "test_singleton_config_fallback.py",
    "test_voicemail_store.py",
    "test_networked_reminder_store.py",
    "test_proactive_engine.py",
    "test_intent_evolution.py",
    "test_reskin_evolution.py",
    "test_voiceprint_mock_chain.py",
    "test_bargein_semantic_gate.py",
    "test_media_iot_tools.py",
    "test_mcp_endpoint_evolution.py",
    "test_iot_executor_evolution.py",
    "test_vllm_evolution.py",
    "test_unified_handler_evolution.py",
    "test_server_mcp_evolution.py",
    "test_device_mcp_evolution.py",
    "test_asyncio_util.py",
    "test_executor_isolation.py",
    "test_tts_join_evolution.py",
    "test_tts_loop_reuse.py",
    "test_shared_chat_executor.py",
    "test_dialogue_dyn_cache.py",
    "test_tencent_tts_evolution.py",
    "test_asr_tencent_evolution.py",
    "test_offline_fallback_evolution.py",
    "test_ab_test_evolution.py",
    "test_dialogue_eval_evolution.py",
    "test_llm_base_evolution.py",
    "test_memory_merge_evolution.py",
    "test_persona_evolution.py",
    "test_persona_api_evolution.py",
    "test_family_rag_evolution.py",
    "test_family_rag_st_migration.py",
    "test_family_knowledge_api_evolution.py",
    "test_plugin_executor_evolution.py",
    "test_unified_tool_manager_evolution.py",
    "test_assets_selfcheck.py",
    "test_dual_config_trap.py",
    "test_metrics_declare.py",
    "test_device_overrides_routes.py",
    "test_conn_uid_dual_ws.py",
    "test_get_weather_adapter.py",
    "test_contract_facts.py",
    "test_schedule_mirror_store.py",
    "test_schedule_downlink.py",
]

# 在线测试（部署后容器侧跑，这里只提醒）
ONLINE_TESTS = [
    "test_fuguoai_backend.py", "test_m7.py", "test_m13_devices_panel.py",
    "test_m14_e2e.py", "test_m14_events.py", "test_m15_presets.py",
    "test_agent_config_ownership.py", "test_device_access.py",
    "test_fuguoai_resilience.py",
]


def l1_compile() -> list:
    fails = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "node_modules", ".venv")]
        for f in files:
            if f.endswith(".py"):
                p = os.path.join(root, f)
                try:
                    py_compile.compile(p, doraise=True)
                except Exception as e:
                    fails.append(f"{os.path.relpath(p, REPO)}: {e}")
    return fails


def l2_import() -> list:
    fails = []
    for mod in IMPORT_SMOKE:
        r = subprocess.run(
            [sys.executable, "-c", f"import {mod}"],
            capture_output=True, text=True, cwd=REPO,
        )
        if r.returncode != 0:
            # 部署前冒烟：路由/关键模块必须能干净导入。任何非零退出码（含漏写
            # import 引发的 NameError、循环依赖等导入期错误）都算 FAIL，不再只认
            # 特定关键字——否则会像 events_routes 那样漏网。
            tail = (r.stderr or "").strip().splitlines()
            last = tail[-1] if tail else "unknown"
            fails.append(f"{mod}: {last}")
    return fails


def l3_tests() -> list:
    fails = []
    env = dict(os.environ, PYTHONPATH=REPO)  # 测试脚本已归集到 tests/，需把仓根加入导入路径
    for t in OFFLINE_TESTS:
        path = os.path.join(REPO, "tests", t)
        if not os.path.exists(path):
            fails.append(f"{t}: 文件不存在")
            continue
        r = subprocess.run([sys.executable, path], capture_output=True, text=True, cwd=REPO, env=env, timeout=300)
        if r.returncode != 0:
            tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-5:])
            fails.append(f"{t}: exit={r.returncode}\n{tail}")
    return fails


def main() -> int:
    quick = "--quick" in sys.argv
    total_fail = 0

    print("=== L1 全仓 py_compile ===")
    f1 = l1_compile()
    print("PASS" if not f1 else "FAIL:\n  " + "\n  ".join(f1))
    total_fail += len(f1)

    print("=== L2 import 冒烟 ===")
    f2 = l2_import()
    print("PASS" if not f2 else "FAIL:\n  " + "\n  ".join(f2))
    total_fail += len(f2)

    if not quick:
        print("=== L3 离线 P0 测试 ===")
        f3 = l3_tests()
        print("PASS" if not f3 else "FAIL:\n  " + "\n  ".join(f3))
        total_fail += len(f3)

    print("\n[提示] 在线回归（部署后容器侧执行）:", ", ".join(ONLINE_TESTS))
    print(f"\n结果: {'ALL PASS ✅' if total_fail == 0 else f'{total_fail} 项 FAIL ❌ — 禁止部署'}")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
