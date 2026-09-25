// D-6 设置页逻辑：保存到 chrome.storage.local（不用 storage.sync）。
const SETTINGS_KEY = "hindsightCollectorSettings";
const $ = (id) => document.getElementById(id);

chrome.storage.local.get(SETTINGS_KEY).then(({ [SETTINGS_KEY]: s }) => {
  s = s || {};
  $("endpoint").value = s.endpoint || "";
  $("ingestToken").value = s.ingestToken || "";
  $("cfClientId").value = s.cfClientId || "";
  $("cfClientSecret").value = s.cfClientSecret || "";
  $("paused").checked = Boolean(s.paused);
  $("backfillSince").value = s.backfillSince || "";
});

$("save").addEventListener("click", async () => {
  const endpoint = $("endpoint").value.trim().replace(/\/$/, "");
  let origin = "";
  try {
    origin = endpoint ? new URL(endpoint).origin : "";
  } catch (_) {
    $("saved").textContent = "地址格式不对（要 https:// 开头），未保存";
    return;
  }
  // 请求对用户填的 Hindsight 地址的跨域权限（optional host permission）
  if (origin && !await chrome.permissions.contains({ origins: [origin + "/*"] })) {
    const granted = await chrome.permissions.request({ origins: [origin + "/*"] });
    if (!granted) {
      $("saved").textContent = "未授予站点权限，未保存";
      return;
    }
  }
  await chrome.storage.local.set({
    [SETTINGS_KEY]: {
      endpoint,
      ingestToken: $("ingestToken").value.trim(),
      cfClientId: $("cfClientId").value.trim(),
      cfClientSecret: $("cfClientSecret").value.trim(),
      paused: $("paused").checked,
      backfillSince: $("backfillSince").value.trim(),
      namespace: "main", // 固定 main：与历史导出按消息 ID 去重（任务书硬性要求）
    },
  });
  $("saved").textContent = "已保存 ✓";
  setTimeout(() => { $("saved").textContent = ""; }, 2000);
});
