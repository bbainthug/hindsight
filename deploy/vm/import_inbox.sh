#!/usr/bin/env bash
# D-5：VM 侧就地导入 inbox 批次（替代整库推送）。
#
# - 按文件名顺序处理 $HINDSIGHT_INBOX/<source>/*.json，每个批次单独导入：
#   批次复制到临时 stage 目录（导入器按目录成员内容做幂等摘要），
#   `brain --db … import-chatgpt <stage> --source <source>`；
#   成功移 inbox/done/<source>/，失败移 inbox/failed/<source>/ 并记日志，
#   失败不阻塞后续批次。
# - flock 防止两次运行重叠（重入直接退出 0，等下一轮 timer）。
# - 单批次顺序导入，不并行（1 GB 内存机器）；导入与 hindsight-mcp 读服务
#   并存（WAL 模式），不重启读服务。
# - 导入后如语义索引已存在，跑一次 reindex-semantic 增量
#   （HINDSIGHT_SEMANTIC_REINDEX=0 可关闭）。
# - 日志只记批次名、事件数、耗时，不记会话正文。
#
# 环境变量：HINDSIGHT_INBOX、HINDSIGHT_VM_DB、HINDSIGHT_VM_ARCHIVE_DIR、
# HINDSIGHT_BRAIN、HINDSIGHT_SEMANTIC_REINDEX。
set -uo pipefail

INBOX="${HINDSIGHT_INBOX:-$HOME/brain-data/inbox}"
DB="${HINDSIGHT_VM_DB:-$HOME/brain-data/db/brain.sqlite}"
ARCHIVE_DIR="${HINDSIGHT_VM_ARCHIVE_DIR:-$HOME/brain-data/archives}"
BRAIN="${HINDSIGHT_BRAIN:-$HOME/hindsight/.venv/bin/brain}"
SEMANTIC_REINDEX="${HINDSIGHT_SEMANTIC_REINDEX:-1}"

mkdir -p "$INBOX/done" "$INBOX/failed"

# 防重叠：Linux 用 flock（任务书要求）；macOS/BSD 无 flock(1)，退回 mkdir
# 原子锁（锁目录超过 30 分钟视为崩溃残留，抢占）。
LOCKDIR=""
acquire_lock() {
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$INBOX/.import.lock"
    flock -n 9
    return $?
  fi
  local lockdir="$INBOX/.import.lockdir"
  if mkdir "$lockdir" 2>/dev/null; then
    LOCKDIR="$lockdir"
    return 0
  fi
  if [[ -z "$(find "$lockdir" -maxdepth 0 -mmin -30 2>/dev/null)" ]]; then
    rm -rf "$lockdir"
    mkdir "$lockdir" 2>/dev/null && { LOCKDIR="$lockdir"; return 0; }
  fi
  return 1
}

if ! acquire_lock; then
  echo "上一轮导入仍在进行，本轮跳过"
  exit 0
fi
if [[ -n "$LOCKDIR" ]]; then
  trap 'rmdir "$LOCKDIR" 2>/dev/null' EXIT
fi

now_ms() {  # GNU date 有 %3N；BSD/macOS 没有（会输出带尾巴的值）→ 退回秒精度
  local v
  v="$(date +%s%3N 2>/dev/null)"
  if [[ "$v" =~ ^[0-9]+$ ]]; then
    echo "$v"
  else
    echo "$(date +%s)000"
  fi
}

# 是否已有语义索引（meta 有行）；无 python 或查询失败都按"未建"处理
semantic_index_exists() {
  local py
  py="$(dirname "$BRAIN")/python"
  [[ -x "$py" ]] || py="python3"
  "$py" - "$DB" <<'PY' 2>/dev/null
import sqlite3, sys
try:
    conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
    row = conn.execute("SELECT 1 FROM semantic_index_meta LIMIT 1").fetchone()
    print("yes" if row else "no")
except Exception:
    print("no")
PY
}

batch_count_total=0
batch_count_failed=0

# 全部来源子目录下的批次，按完整路径排序（路径以 UTC 时间戳开头，全局时序）。
# 排除崩溃残留的 .stage.* 临时目录与 done/failed（深度 3，本就被 maxdepth 挡住）。
batches="$(find "$INBOX" -mindepth 2 -maxdepth 2 -type f -name '*.json' \
  -not -path "$INBOX/.stage.*" | LC_ALL=C sort)"
if [[ -z "$batches" ]]; then
  echo "inbox 无待导入批次"
  exit 0
fi

while IFS= read -r batch; do
  [[ -f "$batch" ]] || continue
  source="$(basename "$(dirname "$batch")")"
  name="$(basename "$batch")"
  stage="$(mktemp -d "$INBOX/.stage.XXXXXX")"
  t0="$(now_ms)"
  cp "$batch" "$stage/$name"
  # nice 让 CPU 优先给读服务（1 vCPU 小机：86MB 级批次解析会吃满核）
  if out="$(nice -n 10 "$BRAIN" --db "$DB" --archive-dir "$ARCHIVE_DIR" \
        import-chatgpt "$stage" --source "$source" 2>&1)"; then
    mkdir -p "$INBOX/done/$source"
    mv "$batch" "$INBOX/done/$source/"
    t1="$(now_ms)"
    events="$(printf '%s' "$out" | grep -o '新事件 [0-9]*' | grep -o '[0-9]*' | tail -1)"
    events="${events:-0}"
    echo "导入成功 [$source] $name  新事件 ${events}  耗时 $((t1 - t0))ms"
    batch_count_total=$((batch_count_total + 1))
  else
    mkdir -p "$INBOX/failed/$source"
    mv "$batch" "$INBOX/failed/$source/"
    echo "导入失败 [$source] $name：$(printf '%s' "$out" | tail -c 200 | tr '\n' ' ')" >&2
    batch_count_total=$((batch_count_total + 1))
    batch_count_failed=$((batch_count_failed + 1))
  fi
  rm -rf "$stage"
done <<< "$batches"

echo "本轮批次 $batch_count_total 个（失败 $batch_count_failed）"

# 语义索引增量（导入有新增时收益；无变化时 reindex 幂等返回 0 新增）
if [[ "$SEMANTIC_REINDEX" == "1" && "$batch_count_failed" -lt "$batch_count_total" ]] \
   && [[ "$(semantic_index_exists)" == "yes" ]]; then
  t0="$(now_ms)"
  if "$BRAIN" --db "$DB" reindex-semantic >/dev/null 2>&1; then
    echo "reindex-semantic 增量完成，耗时 $(($(now_ms) - t0))ms"
  else
    echo "reindex-semantic 失败（不影响导入结果；下轮重试）" >&2
  fi
fi
