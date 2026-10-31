#!/bin/bash
# 复果AI 批量部署脚本 —— conn_uid 修复（对讲双 WS 复用 session_id 注册防串扰，2026-08-02）。
# 铁律链: 本地py_compile → 逐文件scp → 逐文件docker cp+清pycache → 统一restart → 逐文件md5核实 → docker commit。
# 仅部署源文件；tests 不进容器（仅门禁本地用）。
# 关键：4 文件高度耦合（connection.py 传 conn_uid ⇄ connection_registry.py 接收），一次性全量拷贝后统一 restart，
#       避免逐文件 restart 造成的「部分文件新版 + 部分旧版」运行期不一致窗口。
set -e
REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOST="ubuntu@<PROD_HOST>"
KEY="$HOME/.ssh/id_ed25519"
CTN="fuguoai-server"
TAG="${1:-conn-uid-$(date +%Y%m%d-%H%M)}"

FILES=(
  "fuguoai_core/utils/connection_registry.py"
  "fuguoai_core/connection.py"
  "fuguoai_core/handle/helloHandle.py"
  "fuguoai_core/utils/intercom_registry.py"
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
