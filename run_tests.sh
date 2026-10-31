#!/usr/bin/env bash
# 复果AI 后端 · 发现式测试 runner
# 运行 /opt/fuguoai-server 下所有 test_*.py（含新增的 test_m7.py / test_m15_presets.py 等）。
# 用法（容器内）：
#   bash run_tests.sh
# 机制：新增的测试只要命名为 test_*.py 放进该目录，下次 run_tests.sh 即自动纳入回归，
#       无需改动本文件——这是"新增测试进后端"的统一入口。
set -u
cd /opt/fuguoai-server || { echo "无法进入 /opt/fuguoai-server"; exit 1; }
export PYTHONPATH="/opt/fuguoai-server"   # 测试脚本已归集到 tests/，仓根需入导入路径

pass=0
fail=0
failed_files=""
order=0

for t in tests/test_*.py; do
  [ -f "$t" ] || continue
  order=$((order + 1))
  log="/tmp/run_$(basename "$t").log"
  printf "[%02d] RUN  %s ...\n" "$order" "$t"
  if python3 "$t" >"$log" 2>&1; then
    echo "     [PASS] $t"
    pass=$((pass + 1))
  else
    echo "     [FAIL] $t  (详见 $log)"
    fail=$((fail + 1))
    failed_files="$failed_files $t"
  fi
done

echo "=================================================="
echo "SUMMARY: total=$order pass=$pass fail=$fail"
if [ "$fail" -ne 0 ]; then
  echo "FAILED FILES:$failed_files"
fi
exit "$fail"
