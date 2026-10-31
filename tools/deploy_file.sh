#!/bin/bash
# 复果AI 单文件部署脚本 —— 固化部署铁律命令链，防漏步。
# 用法: bash tools/deploy_file.sh <仓内相对路径> [commit-tag]
#   例: bash tools/deploy_file.sh fuguoai_core/utils/intercom_registry.py intercom-20ms-20260729
# 铁律链: 本地py_compile → scp → docker cp → 清__pycache__ → restart → 活文件md5核实 → docker commit
# 注意: 只部署、不 git commit（代码入库仍手动走 git，保证 commit message 质量）。
set -e
REPO="$(cd "$(dirname "$0")/.." && pwd)"
FILE="$1"
TAG="${2:-deploy-$(date +%Y%m%d-%H%M)}"
HOST="ubuntu@<PROD_HOST>"
KEY="$HOME/.ssh/id_ed25519"
CTN="fuguoai-server"

[ -z "$FILE" ] && { echo "用法: $0 <仓内相对路径> [commit-tag]"; exit 1; }
[ -f "$REPO/$FILE" ] || { echo "文件不存在: $REPO/$FILE"; exit 1; }

echo "== 1/6 本地编译校验 =="
python3 -m py_compile "$REPO/$FILE" && echo COMPILE_OK

echo "== 2/6 scp 到跳板目录 =="
BASE="$(basename "$FILE")"
scp -i "$KEY" "$REPO/$FILE" "$HOST:/home/ubuntu/$BASE"

echo "== 3/6 docker cp + 清 __pycache__ =="
DIR="$(dirname "$FILE")"
ssh -i "$KEY" "$HOST" "sudo -n docker cp /home/ubuntu/$BASE $CTN:/opt/fuguoai-server/$FILE && \
  sudo -n docker exec $CTN sh -c 'find /opt/fuguoai-server/$DIR -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null; true'"

echo "== 4/6 restart =="
ssh -i "$KEY" "$HOST" "sudo -n docker restart $CTN" && sleep 5

echo "== 4.5/6 健康检查 (/metrics 秒回 200) =="
HEALTHY=0
for i in $(seq 1 15); do
  # 在远端本机探 8002 /metrics（无鉴权内网端点），只取 http_code，不下载 body
  CODE=$(ssh -i "$KEY" "$HOST" "curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://localhost:8002/metrics" 2>/dev/null || echo "000")
  if [ "$CODE" = "200" ]; then HEALTHY=1; echo "METRICS_OK ✅ http=$CODE try=$i"; break; fi
  echo "  健康检查未就绪 http=$CODE，重试 $i/15..."; sleep 2
done
if [ "$HEALTHY" != "1" ]; then
  echo "METRICS_UNHEALTHY ❌ 服务未在预期时间内就绪，已停止固化镜像（不 commit 坏镜像）。"
  echo "  排查：ssh $HOST 'sudo -n docker logs --tail 50 $CTN'；必要时回滚到上一可用镜像 tag。"
  exit 1
fi

echo "== 5/6 活文件核实 (md5 对比) =="
LOCAL_MD5=$(md5 -q "$REPO/$FILE" 2>/dev/null || md5sum "$REPO/$FILE" | cut -d' ' -f1)
REMOTE_MD5=$(ssh -i "$KEY" "$HOST" "sudo -n docker exec $CTN md5sum /opt/fuguoai-server/$FILE" | cut -d' ' -f1)
if [ "$LOCAL_MD5" = "$REMOTE_MD5" ]; then echo "MD5_MATCH ✅ $LOCAL_MD5"; else echo "MD5_MISMATCH ❌ local=$LOCAL_MD5 remote=$REMOTE_MD5"; exit 1; fi
ssh -i "$KEY" "$HOST" "sudo -n docker ps --format '{{.Names}} {{.Status}}' | grep $CTN"

echo "== 6/6 docker commit 固化镜像 =="
ssh -i "$KEY" "$HOST" "sudo -n docker commit $CTN fuguoai-server:$TAG" && echo "IMAGE=fuguoai-server:$TAG"

echo "== 完成。别忘了本地 git commit + push =="
