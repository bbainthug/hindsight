#!/usr/bin/env bash
# 把 agent_sync 装成 macOS launchd 定时任务（每 15 分钟采集；可选每 30 分钟推到 VM）。
# 脚本会被复制到 $BRAIN_HOME/agent_sync/ —— launchd 无权读 ~/Documents 等受 TCC 保护的目录，
# 所以私有数据根和脚本都放在 ~/.local/share 下。
#
# 用法：install-launchd.sh [--brain-home DIR] [--brain CMD] [--vm-host SSH别名]
#   --vm-host 给了才装 vm-push；SSH 别名要先在 ~/.ssh/config 里配好（建议走 Tailscale）。
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BRAIN_HOME="${BRAIN_HOME:-$HOME/.local/share/personal-brain}"
BRAIN_CLI="${BRAIN_CLI:-$(command -v brain || true)}"
VM_HOST=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --brain-home) BRAIN_HOME="$2"; shift 2;;
    --brain) BRAIN_CLI="$2"; shift 2;;
    --vm-host) VM_HOST="$2"; shift 2;;
    *) echo "未知参数 $1" >&2; exit 2;;
  esac
done
[[ -n "$BRAIN_CLI" ]] || { echo "找不到 brain 命令；用 --brain 指定（例如 <repo>/.venv/bin/brain）" >&2; exit 1; }

mkdir -p "$BRAIN_HOME/agent_sync" "$HOME/Library/LaunchAgents"
cp "$HERE/sync_agents.py" "$BRAIN_HOME/agent_sync/"
cp "$HERE/../../deploy/vm/push_to_vm.py" "$BRAIN_HOME/agent_sync/" 2>/dev/null || true

render() {  # $1=模板 $2=目标
  sed -e "s#__BRAIN_HOME__#$BRAIN_HOME#g" -e "s#__BRAIN_CLI__#$BRAIN_CLI#g" -e "s#__VM_HOST__#$VM_HOST#g" "$1" > "$2"
}
load() {  # $1=label
  launchctl unload "$HOME/Library/LaunchAgents/$1.plist" 2>/dev/null || true
  launchctl load "$HOME/Library/LaunchAgents/$1.plist"
  echo "已加载 $1"
}
render "$HERE/launchd/local.hindsight.agent-sync.plist.tmpl" "$HOME/Library/LaunchAgents/local.hindsight.agent-sync.plist"
load local.hindsight.agent-sync
if [[ -n "$VM_HOST" ]]; then
  render "$HERE/launchd/local.hindsight.vm-push.plist.tmpl" "$HOME/Library/LaunchAgents/local.hindsight.vm-push.plist"
  load local.hindsight.vm-push
fi
echo "日志：$BRAIN_HOME/agent_sync/sync.log（采集）$([[ -n "$VM_HOST" ]] && echo "、vm_push.log（推送）")"
