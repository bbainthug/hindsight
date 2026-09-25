// D-6 纯逻辑测试：node --test integrations/chatgpt_collector/tests/
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  needsFetch, isTemporary, isExcluded, pickForCrawl, nextWaterline, toEpochSeconds,
  pendingUploads, packBatches, nextRetry, resetRetry, canRetryNow,
  markUploaded, MAX_BODY_BYTES, CRAWL_GAP_MS, CRAWL_MAX_PER_ROUND,
} from "../lib/core.js";

const NOW = 1_700_000_000;

test("needsFetch：无本地记录 → 拉；有且无更新 → 不拉", () => {
  const remote = { id: "a", update_time: 100 };
  assert.equal(needsFetch(remote, null), true);
  assert.equal(
    needsFetch(remote, { update_time: 100, uploaded_update_time: null }),
    false,
  );
  assert.equal(
    needsFetch({ id: "a", update_time: 101 }, { update_time: 100, uploaded_update_time: null }),
    true,
  );
});

test("needsFetch：已上传版本 ≥ 远端 → 不拉（避免已上传内容反复重传）", () => {
  const stored = { update_time: 100, uploaded_update_time: 105 };
  assert.equal(needsFetch({ id: "a", update_time: 104 }, stored), false);
  assert.equal(needsFetch({ id: "a", update_time: 106 }, stored), true);
});

test("needsFetch：排除列表内一律不拉", () => {
  const excluded = new Set(["ex-1"]);
  assert.equal(needsFetch({ id: "ex-1", update_time: 200 }, null, excluded), false);
});

test("needsFetch：update_time 缺失或非法 → 不拉", () => {
  assert.equal(needsFetch({ id: "a" }, null), false);
  assert.equal(needsFetch({ id: "a", update_time: "oops" }, null), false);
});

test("isTemporary：显式 is_temporary 才跳过", () => {
  assert.equal(isTemporary({ is_temporary: true }), true);
  assert.equal(isTemporary({ is_temporary: 1 }), true);
  assert.equal(isTemporary({}), false);
  assert.equal(isTemporary({ is_temporary: false }), false);
  assert.equal(isTemporary(null), false);
});

test("isExcluded：支持 id 字符串与对象", () => {
  const set = new Set(["x"]);
  assert.equal(isExcluded("x", set), true);
  assert.equal(isExcluded({ conversation_id: "x" }, set), true);
  assert.equal(isExcluded("y", set), false);
  assert.equal(isExcluded("x", null), false);
});

test("pickForCrawl：从旧到新、过滤排除/水位线、单轮 ≤20", () => {
  const items = Array.from({ length: 25 }, (_, i) => ({
    id: `c${i}`,
    update_time: 1000 + i,
  }));
  items.push({ id: "ex", update_time: 9999 });
  items.push({ id: "old", update_time: 500 });
  const excluded = new Set(["ex"]);
  const picks = pickForCrawl(items, null, excluded, 900);
  assert.equal(picks.length, CRAWL_MAX_PER_ROUND);
  assert.equal(picks[0].id, "c0"); // 最旧的先拉
  assert.ok(picks[0].update_time <= picks[picks.length - 1].update_time);
  assert.ok(!picks.some((p) => p.id === "ex"));
  assert.ok(!picks.some((p) => p.id === "old")); // 低于水位线
});

test("nextWaterline：取本轮实际拉取项的最大 update_time", () => {
  assert.equal(nextWaterline([{ update_time: 5 }, { update_time: 9 }], 7), 9);
  assert.equal(nextWaterline([], 7), 7);
  assert.equal(nextWaterline([{ id: "x" }], null), 0);
});

test("pendingUploads：未传过或有更新才算待传", () => {
  const recs = [
    { json: {}, update_time: 10, uploaded_update_time: null },
    { json: {}, update_time: 10, uploaded_update_time: 10 },
    { json: {}, update_time: 11, uploaded_update_time: 10 },
  ];
  assert.equal(pendingUploads(recs).length, 2);
});

test("packBatches：按字节上限分组", () => {
  const big = "x".repeat(600_000);
  const recs = Array.from({ length: 50 }, (_, i) => ({
    json: { conversation_id: `c${i}`, mapping: {}, blob: big },
    update_time: i,
  }));
  const batches = packBatches(recs);
  assert.ok(batches.length > 1);
  for (const b of batches) {
    const bytes = new TextEncoder().encode(JSON.stringify(b.map((r) => r.json))).length;
    if (b.length > 1) assert.ok(bytes <= MAX_BODY_BYTES);
  }
  const total = batches.reduce((n, b) => n + b.length, 0);
  assert.equal(total, recs.length); // 不丢不重
});

test("packBatches：单个超大对话独立成批（交由服务端 400 拒绝，不静默丢弃）", () => {
  const huge = { conversation_id: "h", mapping: {}, blob: "y".repeat(MAX_BODY_BYTES + 1) };
  const batches = packBatches([{ json: huge, update_time: 1 }]);
  assert.equal(batches.length, 1);
});

test("重试状态机：指数退避、上限 60 分钟、成功清零", () => {
  let s = resetRetry();
  assert.ok(canRetryNow(s, NOW));
  s = nextRetry(s, NOW);
  assert.equal(s.attempts, 1);
  assert.ok(!canRetryNow(s, NOW + 60_000));        // 1 分钟内不重试
  assert.ok(canRetryNow(s, NOW + 2 * 60_000));     // 2 分钟后可
  for (let i = 0; i < 10; i++) s = nextRetry(s, NOW);
  assert.ok(s.next_retry_at - NOW <= 60 * 60_000 + 1); // 退避封顶 60 分钟
  assert.ok(s.attempts === 11);
});

test("markUploaded：记录 uploaded_update_time 与时间", () => {
  const r = { update_time: 42, json: {} };
  const marked = markUploaded(r, NOW);
  assert.equal(marked.uploaded_update_time, 42);
  assert.equal(marked.uploaded_at, NOW);
  assert.equal(r.uploaded_update_time, undefined); // 不改原对象
});

test("节流常量与任务书一致，不得放宽", () => {
  assert.equal(CRAWL_MAX_PER_ROUND, 20);
  assert.equal(CRAWL_GAP_MS, 2000);
  assert.equal(MAX_BODY_BYTES, 16_000_000);
  assert.ok(MAX_BODY_BYTES < 20_000_000);
});

test("isTemporary：ChatGPT 当前字段 is_temporary_chat 也跳过", () => {
  assert.equal(isTemporary({ is_temporary_chat: true }), true);
  assert.equal(isTemporary({ is_temporary_chat: false }), false);
});

test("toEpochSeconds：列表接口的无时区 ISO 字符串按 UTC 解析，数字原样", () => {
  assert.equal(toEpochSeconds(1790318483.184349), 1790318483.184349);
  assert.ok(Math.abs(toEpochSeconds("2026-09-25T06:41:23.18434") - 1790318483.184) < 0.01);
  assert.ok(Math.abs(toEpochSeconds("2026-09-25T06:41:23.18434Z") - 1790318483.184) < 0.01);
  assert.equal(toEpochSeconds(null), null);
  assert.equal(toEpochSeconds("not a date"), null);
});

test("pickForCrawl：真实列表形状（ISO 字符串时间、临时聊天）", () => {
  const items = [
    { id: "a", update_time: "2026-09-25T06:41:23.18434" },
    { id: "b", update_time: "2026-09-24T01:00:00" },
    { id: "t", update_time: "2026-09-25T07:00:00", is_temporary_chat: true },
  ];
  const picks = pickForCrawl(items, null, new Set(), null);
  assert.deepEqual(picks.map((p) => p.id), ["b", "a"]);
  assert.equal(typeof picks[0].update_time, "number");
});

test("截断时不跳过：单轮只拉 20 个，水位线只推进到已拉的最大值", () => {
  const items = Array.from({ length: 28 }, (_, i) => ({ id: `c${i}`, update_time: 1000 + i }));
  const round1 = pickForCrawl(items, null, new Set(), null);
  const w1 = nextWaterline(round1, null);
  const round2 = pickForCrawl(items, null, new Set(), w1);
  assert.equal(round1.length + round2.length, 28);
  assert.ok(!round2.some((p) => round1.includes(p)));
});
