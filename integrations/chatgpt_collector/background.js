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
  backfillSince: "",       // 往回补的起始日期 YYYY-MM-DD，空 = 只采最新
  namespace: "main",       // 任务书硬性要求：namespace 固定 main，与历史导出去重
};

const CRAWL_ALARM = "hindsight-crawl";
const UPLOAD_ALARM = "hindsight-upload";
const TRACKED_TABS_KEY = "hindsightCrawlTabs"; // storage.session：浏览器重启即清空，避免 ID 复用误关

// 本次 SW 生命周期内是否有补采在跑（防止定时与"立即同步"并发各开一个标签页）
let crawlRunning = null;

// 孤儿页清理与补采串行：补采开始前先等清理结束，清理不会关掉补采刚开的标签页。
let cleanupPromise = Promise.resolve();
function scheduleCleanup() {
  if (!crawlRunning) {
    cleanupPromise = cleanupPromise.then(() => closeOrphanTabs()).catch(() => {});
  }
  return cleanupPromise;
}

// SW 每次被唤醒都会重新执行模块顶层：上一个 SW 实例开的补采页此刻已无人负责，补关。
scheduleCleanup();

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(CRAWL_ALARM, { periodInMinutes: core.CRAWL_INTERVAL_MIN });
  chrome.alarms.create(UPLOAD_ALARM, { periodInMinutes: core.UPLOAD_INTERVAL_MIN });
});
chrome.runtime.onStartup.addListener(() => {
  // 浏览器重启后恢复出来的补采页（URL 带标记）也一并关掉
  scheduleCleanup();
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
function runCrawl() {
  if (!crawlRunning) {
    crawlRunning = crawlOnce().finally(() => { crawlRunning = null; });
  }
  return crawlRunning;
}

async function crawlOnce() {
  const settings = await loadSettings();
  if (settings.paused) return;
  const excluded = new Set(await db.allExcluded());
  const waterline = (await db.getMeta("waterline")) ?? null;
  let tab;
  // 长任务期间每 20 秒调一次扩展 API，重置 SW 空闲计时，避免中途被终止而留下标签页
  const keepAlive = setInterval(() => chrome.runtime.getPlatformInfo(() => {}), 20_000);
  const deadline = Date.now() + core.CRAWL_DEADLINE_MS;
  try {
    // 总是自己开一个后台标签页，用完关掉。不复用用户已开的 chatgpt.com 标签页：
    // 扩展安装 / 更新之前打开的页面里没有注入 content script（会报
    // "Receiving end does not exist"），而且不能动用户自己的标签页。
    await cleanupPromise;
    await closeOrphanTabs(); // 自己的标签页还没开，此刻记录里的都是孤儿
    tab = await chrome.tabs.create({ url: core.CRAWL_TAB_URL, active: false });
    await trackTab(tab.id, true);
    await waitTabComplete(tab.id, core.TAB_LOAD_TIMEOUT_MS);
    await sleep(3000); // 页面初始化
    const floor = core.parseBackfillSince(settings.backfillSince);
    const cursor = (await db.getMeta("backfill_cursor")) ?? null;
    // 列表翻页：直到见到"不需要再往下"的时间（向前采：水位线；往回补：起始日期 / 游标），最多 LIST_MAX_PAGES 页
    const stopAt = floor != null ? floor : (waterline ?? Infinity);
    const listedAll = [];
    for (let page = 0; page < core.LIST_MAX_PAGES; page++) {
      checkDeadline(deadline);
      if (page > 0) await sleep(core.CRAWL_GAP_MS); // 节流：列表翻页同样间隔 ≥2s
      const listed = await requestAdapter(tab.id, "crawl", {
        offset: page * core.LIST_PAGE_SIZE, limit: core.LIST_PAGE_SIZE,
      });
      if (listed.error) throw new Error(listed.error);
      const items = listed.conversations || [];
      listedAll.push(...items);
      const last = items.length ? core.toEpochSeconds(items[items.length - 1].update_time) : null;
      if (items.length < core.LIST_PAGE_SIZE || last == null || last <= stopAt) break;
    }
    // 向前采的下界：水位线与起始日期取较晚者（首次运行不去拉起始日期之前的对话）
    const forwardFloor = Math.max(waterline ?? -Infinity, floor ?? -Infinity);
    const forward = core.pickForCrawl(
      listedAll, null, excluded, Number.isFinite(forwardFloor) ? forwardFloor : null,
    );
    const known = new Map((await db.allConversations()).map((r) => [r.conversation_id, r.update_time]));
    const backfill = core.pickBackfill(
      listedAll, excluded, floor, waterline, cursor,
      core.CRAWL_MAX_PER_ROUND - forward.length, known,
    );
    const doneForward = [];
    const doneBackfill = [];
    const queue = [...forward.map((i) => [i, doneForward]), ...backfill.map((i) => [i, doneBackfill])];
    for (let k = 0; k < queue.length; k++) {
      checkDeadline(deadline);
      const [item, doneList] = queue[k];
      const conv = await fetchConversationVia(tab.id, item.id);
      if (conv && !core.isTemporary(conv)) await storeConversation(conv);
      doneList.push(item); // 临时聊天也算"已处理"，水位线 / 游标越过它
      if (k < queue.length - 1) await sleep(core.CRAWL_GAP_MS); // 节流：间隔 ≥2s
    }
    const newWaterline = core.nextWaterline(doneForward, waterline);
    await db.setMeta("waterline", newWaterline);
    // 往回补从"首次水位线"开始往下走；第一次还没有游标时，向前采拉到的最早一条就是起点
    const cursorBase = cursor ?? (doneForward.length ? core.toEpochSeconds(doneForward[0].update_time) : null);
    await db.setMeta("backfill_cursor", core.nextBackfillCursor(doneBackfill, cursorBase));
    await setLastError(null);
    await db.setMeta("last_crawl", Date.now());
  } catch (err) {
    await setLastError(err);
  } finally {
    clearInterval(keepAlive);
    // 只关自己开的那个后台标签页
    if (tab) {
      await chrome.tabs.remove(tab.id).catch(() => {});
      await trackTab(tab.id, false);
    }
  }
}

function checkDeadline(deadline) {
  // 超时时已处理的对话会保存，但本轮水位线不推进，下一轮重取
  if (Date.now() > deadline) throw new Error("crawl deadline exceeded");
}

async function trackedTabIds() {
  return (await chrome.storage.session.get(TRACKED_TABS_KEY))[TRACKED_TABS_KEY] || [];
}

async function trackTab(tabId, add) {
  const ids = (await trackedTabIds()).filter((id) => id !== tabId);
  if (add) ids.push(tabId);
  await chrome.storage.session.set({ [TRACKED_TABS_KEY]: ids });
}

// 关掉无人负责的补采页：记录在案的 ID，或 URL 带补采标记的 chatgpt.com 标签页。
// 只在补采开标签页之前调用（经 scheduleCleanup 或 crawlOnce 开头）。
// 用户自己开的 chatgpt.com 标签页不会被关。
async function closeOrphanTabs() {
  const tracked = await trackedTabIds();
  const tabs = await chrome.tabs.query({ url: "https://chatgpt.com/*" });
  for (const t of tabs) {
    if (core.isCollectorTab(t, tracked)) await chrome.tabs.remove(t.id).catch(() => {});
  }
  await chrome.storage.session.set({ [TRACKED_TABS_KEY]: [] });
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

// 等标签页加载完成；有超时，且先查一次当前状态（complete 事件可能在注册监听前就发生了）
function waitTabComplete(tabId, timeoutMs) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error("chatgpt.com tab load timeout"));
    }, timeoutMs);
    function done() {
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(listener);
      resolve();
    }
    function listener(updatedTabId, info) {
      if (updatedTabId === tabId && info.status === "complete") done();
    }
    chrome.tabs.onUpdated.addListener(listener);
    chrome.tabs.get(tabId).then((t) => { if (t.status === "complete") done(); }, () => {});
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
