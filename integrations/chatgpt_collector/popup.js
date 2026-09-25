// D-6 弹出页：状态展示、立即同步、按对话排除。
const $ = (id) => document.getElementById(id);

function fmtTs(ts) {
  return ts ? new Date(ts).toLocaleString() : "—";
}

async function refresh() {
  const status = await chrome.runtime.sendMessage({ type: "get-status" });
  if (!status) return;
  $("paused").textContent = status.paused ? "已暂停" : (status.endpointConfigured ? "运行中" : "未配置");
  $("pending").textContent = String(status.pendingCount);
  $("stored").textContent = String(status.storedCount);
  $("lastSync").textContent = fmtTs(status.lastSync);
  $("error").textContent = status.lastError
    ? `最近错误：${status.lastError.message}（${fmtTs(status.lastError.at)}）`
    : "";
  const ul = $("recent");
  ul.textContent = "";
  for (const item of status.recent || []) {
    const li = document.createElement("li");
    const t = document.createElement("span");
    t.className = "t";
    t.textContent = item.title || item.conversation_id;
    t.title = item.conversation_id;
    const badge = document.createElement("span");
    badge.className = "badge " + (item.pending ? "pending" : "done");
    badge.textContent = item.pending ? "待传" : "已传";
    const btn = document.createElement("button");
    btn.textContent = "不采";
    btn.addEventListener("click", async () => {
      await chrome.runtime.sendMessage({ type: "exclude", conversationId: item.conversation_id });
      refresh();
    });
    li.append(t, badge, btn);
    ul.append(li);
  }
}

$("syncNow").addEventListener("click", async () => {
  $("syncNow").disabled = true;
  await chrome.runtime.sendMessage({ type: "sync-now" });
  $("syncNow").disabled = false;
  refresh();
});
$("openOptions").addEventListener("click", () => chrome.runtime.openOptionsPage());

refresh();
