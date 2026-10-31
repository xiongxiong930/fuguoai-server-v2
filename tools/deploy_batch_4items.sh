#!/bin/bash
# 复果AI 批量部署脚本 —— 4项遗留功能落地（双配置告警/设备覆盖API/转义修复/指标预声明）。
# 铁律链: 本地py_compile → 逐文件scp → 逐文件docker cp+清pycache → 统一restart → 逐文件md5核实 → docker commit。
# 仅部署源文件；tests 不进容器（仅门禁本地用）。
set -e
REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOST="ubuntu@<PROD_HOST>"
KEY="$HOME/.ssh/id_ed25519"
CTN="fuguoai-server"
TAG="${1:-struct-4items-$(date +%Y%m%d-%H%M)}"

FILES=(
  "app.py"
  "fuguoai_config/config_loader.py"
  "fuguoai_core/api/persona_routes.py"
  "fuguoai_core/utils/metrics.py"
  "fuguoai_core/utils/persona.py"
  "fuguoai_core/api/device_overrides_routes.py"
)

echo "==== 1/5 本地编译校验 ===="
for f in "${FILES[@]}"; do
  python3 -m py_compile "$REPO/$f" && echo "COMPILE_OK $f"
done

echo "==== 2/5 scp + docker cp + 清 __pycache__ ===="
for f in "${FILES[@]}"; do
  BASE="$(basename "$f")"
  DIR="$(dirname "$f")"
  scp -i "$KEY" "$REPO/$f" "$HOST:/home/ubuntu/$BASE"
  ssh -i "$KEY" "$HOST" "sudo -n docker cp /home/ubuntu/$BASE $CTN:/opt/fuguoai-server/$f && \
    sudo -n docker exec $CTN sh -c 'find /opt/fuguoai-server/$DIR -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null; true'"
  echo "COPIED $f"
done

echo "==== 3/5 restart ===="
ssh -i "$KEY" "$HOST" "sudo -n docker restart $CTN" && sleep 6

echo "==== 4/5 逐文件 md5 核实 + 容器 Up ===="
FAIL=0
for f in "${FILES[@]}"; do
  LOCAL_MD5=$(md5 -q "$REPO/$f" 2>/dev/null || md5sum "$REPO/$f" | cut -d' ' -f1)
  REMOTE_MD5=$(ssh -i "$KEY" "$HOST" "sudo -n docker exec $CTN md5sum /opt/fuguoai-server/$f" | cut -d' ' -f1)
  if [ "$LOCAL_MD5" = "$REMOTE_MD5" ]; then echo "MD5_MATCH ✅ $f $LOCAL_MD5"; else echo "MD5_MISMATCH ❌ $f local=$LOCAL_MD5 remote=$REMOTE_MD5"; FAIL=1; fi
done
ssh -i "$KEY" "$HOST" "sudo -n docker ps --format '{{.Names}} {{.Status}}' | grep $CTN"
[ "$FAIL" -eq 0 ] || { echo "MD5 核实失败，终止"; exit 1; }

echo "==== 5/5 docker commit 固化镜像 ===="
ssh -i "$KEY" "$HOST" "sudo -n docker commit $CTN fuguoai-server:$TAG" && echo "IMAGE=fuguoai-server:$TAG"
echo "==== 完成。记得本地 git commit + push ===="
