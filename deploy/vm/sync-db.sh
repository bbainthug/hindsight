#!/usr/bin/env bash
# D-3 远程接入：Mac 侧把本地 brain.sqlite 的一致性快照推到 VM，原子替换后重启服务。
#
# 用 sqlite3 的 .backup 命令（等价于 backup API）在读写不冲突的前提下产出
# 一致性快照，rsync 到 VM 的临时文件，原子 mv 覆盖后重启 systemd --user 服务。
# 不做双向同步——VM 上的库只是 Mac 的单向快照副本（见任务书"已知取舍"）。
#
# 用法：
#   sync-db.sh <本地db路径> <VM host别名或 user@host> [VM 上的目标路径]
# 示例：
#   sync-db.sh ~/brain-data/brain.sqlite hindsight-vm ~/brain-data/brain.sqlite
#
# 可接到现有每日备份 launchd 之后：在 backup 成功后调用本脚本，把同一份快照
# （或从中再 .backup 一次）同步到远端。
set -euo pipefail

LOCAL_DB="${1:?用法: sync-db.sh <本地db路径> <VM host> [远端路径]}"
VM_HOST="${2:?用法: sync-db.sh <本地db路径> <VM host> [远端路径]}"
REMOTE_DB="${3:-brain-data/brain.sqlite}"
SERVICE_NAME="${HINDSIGHT_SERVICE_NAME:-hindsight-mcp}"

if [[ ! -f "$LOCAL_DB" ]]; then
  echo "拒绝：本地库不存在：$LOCAL_DB" >&2
  exit 1
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
SNAPSHOT="$WORKDIR/brain.sqlite.snapshot"

echo "==> sqlite3 .backup 一致性快照（不阻塞本地读写）"
sqlite3 "$LOCAL_DB" ".backup '$SNAPSHOT'"

echo "==> rsync 到 VM 临时文件"
REMOTE_TMP="${REMOTE_DB}.incoming.$$"
rsync -az --checksum "$SNAPSHOT" "${VM_HOST}:${REMOTE_TMP}"

echo "==> 远端原子 mv 覆盖 + 重启服务"
ssh "$VM_HOST" bash -s -- "$REMOTE_TMP" "$REMOTE_DB" "$SERVICE_NAME" <<'REMOTE'
set -euo pipefail
incoming="$1"
target="$2"
service="$3"
mv -f "$incoming" "$target"
systemctl --user restart "$service"
echo "远端已切换到新快照并重启 $service"
REMOTE

echo "==> 完成：$LOCAL_DB -> ${VM_HOST}:${REMOTE_DB}"
