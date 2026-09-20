#!/usr/bin/env bash
# D-3 远程接入：在 VM 上以当前用户（非 root）安装 systemd --user 服务。
#
# 前提：代码已在 ~/hindsight（或通过 --repo-dir 指定），~/brain-data/remote.yaml
# 已就绪，uv 已安装且在 PATH 上。不写任何真实域名/密钥到仓库；token 只落地到
# ~/.config/hindsight/env（600 权限），不进 git。
set -euo pipefail

REPO_DIR="${1:-$HOME/hindsight}"
CONFIG_PATH="${BRAIN_CONFIG_PATH:-$HOME/brain-data/remote.yaml}"
ENV_DIR="$HOME/.config/hindsight"
ENV_FILE="$ENV_DIR/env"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_FILE="$UNIT_DIR/hindsight-mcp.service"

if [[ "$(id -u)" -eq 0 ]]; then
  echo "拒绝：不要以 root 运行 install.sh（服务本身也不以 root 跑，见 hindsight-mcp.service）。" >&2
  exit 1
fi

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "拒绝：找不到 $CONFIG_PATH（先准备好受信任的 remote profile 配置）。" >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "拒绝：找不到 uv（先安装：https://docs.astral.sh/uv/getting-started/installation/）。" >&2
  exit 1
fi

echo "==> uv sync --extra semantic --extra remote（$REPO_DIR）"
(cd "$REPO_DIR" && uv sync --extra semantic --extra remote)

mkdir -p "$ENV_DIR"
chmod 700 "$ENV_DIR"

if [[ -f "$ENV_FILE" ]]; then
  echo "==> $ENV_FILE 已存在，保留现有 BRAIN_MCP_TOKEN（不覆盖）。"
else
  echo "==> 生成 BRAIN_MCP_TOKEN 并写入 $ENV_FILE（600 权限，不进 git）"
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  umask 077
  printf 'BRAIN_MCP_TOKEN=%s\n' "$TOKEN" > "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "已生成 token（不打印到终端历史以外的地方）。查看：cat $ENV_FILE"
fi

mkdir -p "$UNIT_DIR"
# systemd 原生支持单元文件里的 %h（调用用户的 $HOME）展开，直接复制模板即可；
# WorkingDirectory 假定仓库在 %h/hindsight——若 REPO_DIR 不同，请自行调整该行。
if [[ "$REPO_DIR" != "$HOME/hindsight" ]]; then
  echo "提示：REPO_DIR=$REPO_DIR 非默认 ~/hindsight，请手动检查" \
       "$UNIT_FILE 里的 WorkingDirectory。"
fi
cp "$(dirname "$0")/hindsight-mcp.service" "$UNIT_FILE"

echo "==> systemctl --user daemon-reload && enable --now hindsight-mcp"
systemctl --user daemon-reload
systemctl --user enable --now hindsight-mcp

echo "==> loginctl enable-linger（开机/无登录会话时服务仍常驻）"
loginctl enable-linger "$USER" || {
  echo "警告：enable-linger 失败（可能需要 sudo）；请手动执行：sudo loginctl enable-linger $USER" >&2
}

echo "==> 状态："
systemctl --user status hindsight-mcp --no-pager || true

cat <<'EOF'

完成。下一步：
1. 确认服务正常监听回环：curl -s http://127.0.0.1:8765/api/status | head -c 200
2. 配置 cloudflared（见 deploy/vm/cloudflared.example.yml 与 deploy/vm/README.md）
3. MCP 能力 URL：https://<your-domain>/mcp/$(grep BRAIN_MCP_TOKEN ~/.config/hindsight/env | cut -d= -f2)
   （只把这个 URL 给受信任的客户端；泄露 = 该 profile 可见内容泄露，轮换方法见
   docs/remote-access.md）
EOF
