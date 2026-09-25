// D-6 纯逻辑（无 chrome / DOM 依赖，node --test 可直接测）：
// 变更检测、上传打包、排除过滤、重试状态机、节流参数。
// 所有常量按任务书写死，调用方不得放宽。

export const CRAWL_INTERVAL_MIN = 30;       // 补采轮间隔
export const UPLOAD_INTERVAL_MIN = 15;      // 上传轮间隔
export const CRAWL_MAX_PER_ROUND = 20;      // 单轮最多取 20 个对话
export const CRAWL_GAP_MS = 2000;           // 对话详情请求间隔 ≥ 2 秒
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
  return conv?.is_temporary === true || conv?.is_temporary === 1;
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
  const items = (remoteItems || [])
    .filter((it) => it && typeof it.id === "string" && it.id)
    .filter((it) => !isExcluded(it, excluded))
    .filter((it) => typeof it.update_time === "number" && it.update_time > (waterline ?? -Infinity))
    .sort((a, b) => b.update_time - a.update_time);
  return items.slice(0, CRAWL_MAX_PER_ROUND);
}

/** 本轮之后的新水位线：本轮见到的最大 update_time（与已拉取无关）。 */
export function nextWaterline(remoteItems, current) {
  let max = current ?? 0;
  for (const it of remoteItems || []) {
    if (typeof it.update_time === "number" && it.update_time > max) max = it.update_time;
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
