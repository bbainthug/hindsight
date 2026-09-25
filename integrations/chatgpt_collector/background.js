// D-6 service worker：调度（补采每 30 分钟、上传每 15 分钟）、节流、
// 上传 /ingest/main、即时采集落库、状态查询。ChatGPT 会话 token 永远不经过这里
// ——token 只存在于 adapters/chatgpt.js 的页面内存中。
import * as core from "./lib/core.js";
import * as db from "./db.js";

const SETTINGS_KEY = "hindsightCollectorSettings";
const DEFAULT_SETTINGS = {
  endpoint: "",            // 用户填的 Hindsight 地址，如 https://brain.example.com
  ingestToken: "",         // BRAIN_INGEST_TOKEN（Hindsight 写入 token）
  cfClientId: "",          // Cloudflare Access 服务令牌（可选，走 Access 时填）
  cfClientSecret: "",
  paused: false,
  namespace: "main",       // 任务书硬性要求：namespace 固定 main，与历史导出去重
};

const CRAWL_ALARM = "hindsight-crawl";
const UPLOAD_ALARM = "hindsight-upload";

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(CRAWL_ALARM, { periodInMinutes: core.CRAWL_INTERVAL_MIN });
  chrome.alarms.create(UPLOAD_ALARM, { periodInMinutes: core.UPLOAD_INTERVAL_MIN });
});
chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create(CRAWL_ALARM, { periodInMinutes: core.CRAWL_INTERVAL_MIN });
  chrome.alarms.create(UPLOAD_ALARM, { periodInMinutes: core.UPLOAD_INTERVAL_MIN });
});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === CRAWL_ALARM) runCrawl();
  if (alarm.name === UPLOAD_ALARM) runUpload();
});

async function loadSettings() {
  const stored = (await chrome.storage.local.get(SETTINGS_KEY))[SETTINGS_KEY] || {};
  return { ...DEFAULT_SETTINGS, ...stored };
}

async function lastError() {
  return (await db.getMeta("last_error")) || null;
}

async function setLastError(err) {
  await db.setMeta("last_error", err ? { message: String(err).slice(0, 200), at: Date.now() } : null);
}

// ------------------------------------------------------------------
// 补采：打开（或复用）一个后台 chatgpt.com 标签页，按更新时间拉最近
// 对话列表，对"晚于水位线且未排除"的对话逐个取完整 JSON。
// 节流（任务书硬要求，不放宽）：单轮 ≤20 个、请求间隔 ≥2s。
// ------------------------------------------------------------------
async function runCrawl() {
  const settings = await loadSettings();
  if (settings.paused) return;
  const excluded = new Set(await db.allExcluded());
  const waterline = (await db.getMeta("waterline")) ?? null;
  let tab;
  try {
    // 总是自己开一个后台标签页，用完关掉。不复用用户已开的 chatgpt.com 标签页：
    // 扩展安装 / 更新之前打开的页面里没有注入 content script（会报
    // "Receiving end does not exist"），而且不能动用户自己的标签页。
    tab = await chrome.tabs.create({ url: "https://chatgpt.com/", active: false });
    await waitTabComplete(tab.id);
    await sleep(3000); // 页面初始化
    const listed = await requestAdapter(tab.id, "crawl", { offset: 0, limit: 28 });
    if (listed.error) throw new Error(listed.error);
    const picks = core.pickForCrawl(listed.conversations, null, excluded, waterline);
    const done = [];
    for (const item of picks) {
      if (done.length >= core.CRAWL_MAX_PER_ROUND) break; // 节流：单轮上限
      const conv = await fetchConversationVia(tab.id, item.id);
      if (conv && !core.isTemporary(conv)) await storeConversation(conv);
      done.push(item); // 临时聊天也算"已处理"，水位线越过它
      if (done.length < picks.length) await sleep(core.CRAWL_GAP_MS); // 节流：间隔 ≥2s
    }
    await db.setMeta("waterline", core.nextWaterline(done, waterline));
    await setLastError(null);
    await db.setMeta("last_crawl", Date.now());
  } catch (err) {
    await setLastError(err);
  } finally {
    // 只关自己开的那个后台标签页
    if (tab) chrome.tabs.remove(tab.id).catch(() => {});
  }
}

async function fetchConversationVia(tabId, id) {
  const res = await requestAdapter(tabId, "get", { ids: [id] });
  if (res.error) throw new Error(res.error);
  return (res.conversations || [])[0] || null;
}

async function requestAdapter(tabId, adapterType, payload) {
  const reqId = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return chrome.tabs.sendMessage(tabId, {
    type: "adapter-request", adapterType, payload, reqId,
  });
}

function waitTabComplete(tabId) {
  return new Promise((resolve) => {
    function listener(updatedTabId, info) {
      if (updatedTabId === tabId && info.status === "complete") {
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// ------------------------------------------------------------------
// 即时采集（adapter 捕获页面自身请求 → bridge → 这里）
// ------------------------------------------------------------------
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "adapter-conversations") {
    (async () => {
      const excluded = new Set(await db.allExcluded());
      for (const conv of msg.payload?.conversations || []) {
        if (core.isTemporary(conv) || core.isExcluded(conv, excluded)) continue;
        await storeConversation(conv);
      }
      sendResponse({ ok: true });
    })();
    return true;
  }
  if (msg && msg.type === "get-status") {
    (async () => sendResponse(await buildStatus()))();
    return true;
  }
  if (msg && msg.type === "sync-now") {
    (async () => {
      await runCrawl();
      await runUpload();
      sendResponse(await buildStatus());
    })();
    return true;
  }
  if (msg && msg.type === "exclude") {
    (async () => {
      await db.addExcluded(msg.conversationId);
      await db.deleteConversation(msg.conversationId); // 本地删除且不再上传
      sendResponse({ ok: true });
    })();
    return true;
  }
  return false;
});

async function storeConversation(conv) {
  if (!conv || typeof conv.conversation_id !== "string" || !conv.mapping) return;
  const prev = await db.getConversationRecord(conv.conversation_id);
  const updateTime = typeof conv.update_time === "number"
    ? conv.update_time
    : (prev?.update_time ?? null);
  const record = {
    conversation_id: conv.conversation_id,
    title: conv.title || conv.conversation_id,
    update_time: updateTime,
    json: conv,
    uploaded_update_time: prev?.uploaded_update_time ?? null,
    uploaded_at: prev?.uploaded_at ?? null,
    fetched_at: Date.now(),
    retry: prev?.retry || core.resetRetry(),
  };
  // 内容没变就不重写（保持 uploaded 状态）
  if (prev && prev.uploaded_update_time != null
      && updateTime != null && updateTime <= prev.uploaded_update_time) {
    return;
  }
  await db.putConversation(record);
}

// ------------------------------------------------------------------
// 上传：有变化的对话按字节分批 POST /ingest/main；
// 成功才标记 uploaded_update_time，失败按退避状态机保留重试。
// ------------------------------------------------------------------
async function runUpload() {
  const settings = await loadSettings();
  if (settings.paused || !settings.endpoint || !settings.ingestToken) return;
  const records = core.pendingUploads(await db.allConversations());
  const retryable = records.filter((r) => core.canRetryNow(r.retry, Date.now()));
  if (!retryable.length) return;
  const batches = core.packBatches(retryable);
  let lastError = null;
  for (const batch of batches) {
    try {
      await uploadBatch(settings, batch);
      for (const r of batch) {
        await db.putConversation(core.markUploaded(r, Date.now()));
      }
      await db.setMeta("last_sync", Date.now());
      await setLastError(null);
    } catch (err) {
      lastError = err;
      for (const r of batch) {
        await db.putConversation({
          ...r,
          retry: core.nextRetry(r.retry, Date.now()),
        });
      }
    }
  }
  if (lastError) await setLastError(lastError);
}

async function uploadBatch(settings, batch) {
  const conversations = batch.map((r) => r.json);
  const body = JSON.stringify(conversations);
  if (new TextEncoder().encode(body).length > 20_000_000) {
    throw new Error("batch exceeds 20MB"); // packBatches 已按 16MB 分组，防御性断言
  }
  const headers = {
    "Content-Type": "application/json",
    "Authorization": `Bearer ${settings.ingestToken}`,
  };
  if (settings.cfClientId && settings.cfClientSecret) {
    headers["CF-Access-Client-Id"] = settings.cfClientId;
    headers["CF-Access-Client-Secret"] = settings.cfClientSecret;
  }
  const resp = await fetch(
    `${settings.endpoint.replace(/\/$/, "")}/ingest/${settings.namespace}`,
    { method: "POST", headers, body },
  );
  if (resp.status !== 202 && resp.status !== 200) {
    throw new Error(`/ingest → ${resp.status}`);
  }
}

async function buildStatus() {
  const settings = await loadSettings();
  const records = await db.allConversations();
  const pending = core.pendingUploads(records).filter(
    (r) => core.canRetryNow(r.retry, Date.now())
  );
  return {
    paused: settings.paused,
    endpointConfigured: Boolean(settings.endpoint && settings.ingestToken),
    lastSync: (await db.getMeta("last_sync")) || null,
    lastCrawl: (await db.getMeta("last_crawl")) || null,
    lastError: await lastError(),
    pendingCount: pending.length,
    storedCount: records.length,
    recent: records
      .sort((a, b) => (b.update_time ?? 0) - (a.update_time ?? 0))
      .slice(0, 10)
      .map((r) => ({
        conversation_id: r.conversation_id,
        title: r.title,
        update_time: r.update_time,
        pending: core.pendingUploads([r]).length > 0,
      })),
  };
}
