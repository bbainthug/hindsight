// D-6 纯逻辑（无 chrome / DOM 依赖，node --test 可直接测）：
// 变更检测、上传打包、排除过滤、重试状态机、节流参数。
// 所有常量按任务书写死，调用方不得放宽。

export const CRAWL_INTERVAL_MIN = 30;       // 补采轮间隔
export const UPLOAD_INTERVAL_MIN = 15;      // 上传轮间隔
export const CRAWL_MAX_PER_ROUND = 20;      // 单轮最多取 20 个对话
export const CRAWL_GAP_MS = 2000;           // 对话详情请求间隔 ≥ 2 秒（列表翻页同样适用）
export const LIST_PAGE_SIZE = 28;           // 列表每页条数（与 ChatGPT 前端一致）
export const LIST_MAX_PAGES = 10;           // 单轮最多翻 10 页列表
export const MAX_BODY_BYTES = 16_000_000;   // 单次上传批次上限（服务端 20MB，留余量）
export const RETRY_MAX_MIN = 60;            // 重试退避上限

/**
 * 变更检测：给定 IndexedDB 里的已存记录（可空）与远端列表项，
 * 判断是否需要（重新）拉取完整 JSON。
 */
export function needsFetch(remoteItem, stored, excludedSet) {
  const rt = remoteItem && remoteItem.update_time;
  if (typeof rt !== "number" || !Number.isFinite(rt)) return false;
  if (isExcluded(remoteItem, excludedSet)) return false;
  if (!stored) return true;
  if (stored.uploaded_update_time != null && rt <= stored.uploaded_update_time) {
    return false; // 已上传的版本不早于远端 → 无变化
  }
  return rt > (stored.update_time ?? -Infinity);
}

/** 临时聊天判定：is_temporary 显式为 true 即跳过（字段缺失视为正常对话）。 */
export function isTemporary(conv) {
  // ChatGPT 当前字段名是 is_temporary_chat；is_temporary 为旧名 / 兼容
  const flag = (v) => v === true || v === 1;
  return flag(conv?.is_temporary_chat) || flag(conv?.is_temporary);
}

/**
 * 把 update_time 统一成 epoch 秒。列表接口给的是无时区 ISO 字符串
 * （"2026-09-25T06:41:23.18434"，实为 UTC），单个对话接口给的是数字。
 */
export function toEpochSeconds(v) {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v !== "string" || !v) return null;
  const hasZone = /(Z|[+-]\d\d:?\d\d)$/.test(v);
  const ms = Date.parse(hasZone ? v : v + "Z");
  return Number.isFinite(ms) ? ms / 1000 : null;
}

/** 排除列表判定（兼容列表项的 id 与记录的 conversation_id 字段名） */
export function isExcluded(itemOrId, excludedSet) {
  if (!(excludedSet instanceof Set)) return false;
  const id = typeof itemOrId === "string"
    ? itemOrId
    : (itemOrId?.conversation_id ?? itemOrId?.id);
  return excludedSet.has(id);
}

/**
 * 从远端列表挑出本轮要拉的对话：按 update_time 降序、未排除、
 * 晚于水位线，最多 CRAWL_MAX_PER_ROUND 个。
 */
export function pickForCrawl(remoteItems, storedById, excluded, waterline) {
  // 从旧到新拉：单轮上限截断时，没拉到的是较新的那些，下一轮水位线仍低于它们，不会被跳过
  const items = (remoteItems || [])
    .filter((it) => it && typeof it.id === "string" && it.id)
    .filter((it) => !isExcluded(it, excluded) && !isTemporary(it))
    .map((it) => ({ ...it, update_time: toEpochSeconds(it.update_time) }))
    .filter((it) => it.update_time != null && it.update_time > (waterline ?? -Infinity))
    .sort((a, b) => a.update_time - b.update_time);
  return items.slice(0, CRAWL_MAX_PER_ROUND);
}

/** 本轮之后的新水位线：本轮**实际拉取成功**的条目里最大的 update_time。 */
export function nextWaterline(crawledItems, current) {
  let max = current ?? 0;
  for (const it of crawledItems || []) {
    const t = toEpochSeconds(it?.update_time);
    if (t != null && t > max) max = t;
  }
  return max;
}

/** 有变化待上传的记录 */
export function pendingUploads(records) {
  return (records || []).filter(
    (r) => r && r.json && (r.uploaded_update_time == null
      || (typeof r.update_time === "number" && r.update_time > r.uploaded_update_time)),
  );
}

/**
 * 打包上传批次：按字节上限分组（单批 ≤ MAX_BODY_BYTES，
 * 单个对话超限则独立成批并在上传时被服务端 400 拒绝——不静默丢弃）。
 */
export function packBatches(records) {
  const batches = [];
  let cur = [];
  let size = 0;
  for (const r of records || []) {
    const bytes = jsonByteLength(r.json);
    if (cur.length && size + bytes > MAX_BODY_BYTES) {
      batches.push(cur);
      cur = [];
      size = 0;
    }
    cur.push(r);
    size += bytes;
  }
  if (cur.length) batches.push(cur);
  return batches;
}

export function jsonByteLength(value) {
  // 与服务端一致按 UTF-8 字节计
  return new TextEncoder().encode(JSON.stringify(value)).length;
}

/**
 * 重试状态机（"重复尝试但每次轮询统一重试"）：
 * 失败 → attempts+1，next_retry_at = now + min(2^attempts, RETRY_MAX_MIN) 分钟；
 * 成功 → 清零。轮询方只管到点重试。
 */
export function nextRetry(state, nowMs) {
  const attempts = (state?.attempts ?? 0) + 1;
  const backoff = Math.min(2 ** attempts, RETRY_MAX_MIN) * 60_000;
  return { attempts, next_retry_at: (nowMs ?? Date.now()) + backoff };
}

export function resetRetry() {
  return { attempts: 0, next_retry_at: 0 };
}

export function canRetryNow(state, nowMs) {
  return !state || (state.next_retry_at ?? 0) <= (nowMs ?? Date.now());
}

/** 上传成功后标记：记录本次上传覆盖到的 update_time */
export function markUploaded(record, nowMs) {
  return { ...record, uploaded_update_time: record.update_time, uploaded_at: nowMs ?? Date.now() };
}

/**
 * 往回补：挑出 floor < t ≤ ceiling 且严格小于 cursor 的对话，从新到旧，最多 limit 个。
 * - floor：用户设置的起始日期（epoch 秒），null 表示不往回补；
 * - ceiling：本轮开始前的水位线（高于它的归"向前采"管），null 表示不设上限；
 * - cursor：往回补已推进到的最早时间（epoch 秒），null 表示还没开始。
 */
export function pickBackfill(remoteItems, excluded, floor, ceiling, cursor, limit, known) {
  if (floor == null || !(limit > 0)) return [];
  const already = known instanceof Map ? known : new Map();
  return (remoteItems || [])
    .filter((it) => it && typeof it.id === "string" && it.id)
    .filter((it) => !isExcluded(it, excluded) && !isTemporary(it))
    .map((it) => ({ ...it, update_time: toEpochSeconds(it.update_time) }))
    .filter((it) => it.update_time != null && it.update_time > floor)
    .filter((it) => ceiling == null || it.update_time <= ceiling)
    .filter((it) => cursor == null || it.update_time < cursor)
    // 本地已存、且没有更新的对话不再重复拉取
    .filter((it) => !(already.get(it.id) != null && already.get(it.id) >= it.update_time))
    .sort((a, b) => b.update_time - a.update_time)
    .slice(0, limit);
}

/** 往回补推进后的新游标：本轮往回补实际处理条目里最早的时间。 */
export function nextBackfillCursor(doneItems, current) {
  let min = current ?? Infinity;
  for (const it of doneItems || []) {
    const t = toEpochSeconds(it?.update_time);
    if (t != null && t < min) min = t;
  }
  return Number.isFinite(min) ? min : (current ?? null);
}

/** 设置里的日期字符串（YYYY-MM-DD，按 UTC 零点）→ epoch 秒；空或非法返回 null。 */
export function parseBackfillSince(s) {
  if (typeof s !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(s.trim())) return null;
  return toEpochSeconds(s.trim() + "T00:00:00Z");
}
