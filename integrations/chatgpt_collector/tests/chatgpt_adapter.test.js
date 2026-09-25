// D-6 适配器测试：adapters/chatgpt.js 是 MAIN world 经典脚本，
// 这里用 node:vm 加载（注入 fake window / fetch / TextEncoder），
// 内部接口响应全部来自录制的合成 fixture，不连真网站。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");
const ADAPTER_SRC = readFileSync(path.join(ROOT, "adapters/chatgpt.js"), "utf8");
const LIST_FIXTURE = JSON.parse(
  readFileSync(path.join(ROOT, "tests/fixtures/conversations_list.json"), "utf8"),
);
const CONV_FIXTURE = JSON.parse(
  readFileSync(path.join(ROOT, "tests/fixtures/conversation_aaa.json"), "utf8"),
);

const here = "https://chatgpt.com/c/abc";

function loadAdapter(routes) {
  const messages = []; // adapter 发出的 postMessage
  const listeners = new Set();
  const windowObj = {
    location: { origin: "https://chatgpt.com", href: here },
    addEventListener: (type, fn) => { if (type === "message") listeners.add(fn); },
    postMessage: (msg) => messages.push(msg),
    fetch: async (url, init) => {
      const key = typeof url === "string" ? url.split("?")[0] : url.url.split("?")[0];
      const handler = routes[key];
      if (!handler) throw new Error("unexpected fetch: " + key);
      return handler(url, init);
    },
  };
  windowObj.window = windowObj;
  const ctx = {
    window: windowObj,
    fetch: windowObj.fetch,
    TextEncoder,
    URL,
    console,
    setTimeout,
    clearTimeout,
  };
  vm.createContext(ctx);
  vm.runInContext(ADAPTER_SRC, ctx);
  const window = ctx.window;
  return {
    window,
    messages,
    emit(msg) {
      for (const fn of listeners) fn({ source: window, data: msg });
    },
  };
}

function jsonResp(obj, status = 200) {
  return { ok: status < 400, status, json: async () => obj, clone() { return jsonResp(obj, status); } };
}

function standardRoutes() {
  return {
    "https://chatgpt.com/api/auth/session": () =>
      jsonResp({ accessToken: "sess-token-abc" }),
    "https://chatgpt.com/backend-api/conversations": () => jsonResp(LIST_FIXTURE),
    "https://chatgpt.com/backend-api/conversation/conv-aaa-1": () => jsonResp(CONV_FIXTURE),
  };
}

function waitFor(fn, { timeout = 1000 } = {}) {
  return new Promise((resolve, reject) => {
    const started = Date.now();
    (function poll() {
      const v = fn();
      if (v !== undefined) return resolve(v);
      if (Date.now() - started > timeout) return reject(new Error("timeout"));
      setTimeout(poll, 10);
    })();
  });
}

test("adapter：listRecent 走 backend-api/conversations，带 Authorization，token 不落任何存储", async () => {
  let seenAuth = null;
  const routes = standardRoutes();
  routes["https://chatgpt.com/backend-api/conversations"] = (_url, init) => {
    seenAuth = init?.headers?.Authorization;
    return jsonResp(LIST_FIXTURE);
  };
  const a = loadAdapter(routes);
  a.emit({ source: "hindsight-bridge", type: "crawl", payload: { offset: 0, limit: 28 }, reqId: "r1" });
  const msg = await waitFor(() => a.messages.find((m) => m.reqId === "r1"));
  assert.equal(seenAuth, "Bearer sess-token-abc");
  assert.equal(msg.source, "hindsight-adapter");
  assert.equal(msg.payload.conversations.length, 4);
  assert.equal(msg.payload.conversations[0].id, "conv-aaa-1");
  assert.equal(msg.payload.conversations[1].is_temporary, true); // 透传，过滤在 core.js
  // token 只在适配器内存里：window 上没有暴露存储痕迹之外的全局
  assert.ok(!("accessToken" in a.window));
});

test("adapter：getConversation 返回完整导出结构（mapping/current_node/conversation_id）", async () => {
  const a = loadAdapter(standardRoutes());
  a.emit({ source: "hindsight-bridge", type: "get", payload: { ids: ["conv-aaa-1"] }, reqId: "r2" });
  const msg = await waitFor(() => a.messages.find((m) => m.reqId === "r2"));
  const conv = msg.payload.conversations[0];
  assert.equal(conv.conversation_id, "conv-aaa-1");
  assert.ok(conv.mapping && conv.mapping.n1.message.content.parts[0].includes("deploy"));
  assert.equal(conv.current_node, "n2");
});

test("adapter：401 时重取 session 再试一次", async () => {
  let sessionCalls = 0;
  let convCalls = 0;
  const routes = {
    "https://chatgpt.com/api/auth/session": () => {
      sessionCalls += 1;
      return jsonResp({ accessToken: "tok-" + sessionCalls });
    },
    "https://chatgpt.com/backend-api/conversation/conv-aaa-1": (_url, init) => {
      convCalls += 1;
      if (convCalls === 1) return { ok: false, status: 401, json: async () => ({}) };
      assert.equal(init.headers.Authorization, "Bearer tok-2");
      return jsonResp(CONV_FIXTURE);
    },
  };
  const a = loadAdapter(routes);
  a.emit({ source: "hindsight-bridge", type: "get", payload: { ids: ["conv-aaa-1"] }, reqId: "r3" });
  const msg = await waitFor(() => a.messages.find((m) => m.reqId === "r3"));
  assert.equal(msg.payload.conversations[0].conversation_id, "conv-aaa-1");
  assert.equal(sessionCalls, 2);
});

test("adapter：临时聊天不进 get 结果；错误经 postMessage 上报而非抛出", async () => {
  const routes = standardRoutes();
  routes["https://chatgpt.com/backend-api/conversation/conv-aaa-1"] = () =>
    jsonResp({ ...CONV_FIXTURE, is_temporary: true });
  const a = loadAdapter(routes);
  a.emit({ source: "hindsight-bridge", type: "get", payload: { ids: ["conv-aaa-1"] }, reqId: "r4" });
  const msg = await waitFor(() => a.messages.find((m) => m.reqId === "r4"));
  assert.equal(msg.payload.conversations.length, 0); // 被跳过（vm realm 数组不用 deepEqual）
  a.emit({ source: "hindsight-bridge", type: "get", payload: { ids: ["nope"] }, reqId: "r5" });
  const err = await waitFor(() => a.messages.find((m) => m.reqId === "r5"));
  assert.match(err.payload.error, /error|→|unexpected/i);
});

test("adapter：fetch patch 捕获页面自身的对话响应（即时采集），且不吞掉原始响应", async () => {
  const a = loadAdapter(standardRoutes());
  const orig = a.window.fetch;
  // 模拟页面自身发起的对话请求（通过被 patch 的 window.fetch）
  const respPromise = a.window.fetch(
    "https://chatgpt.com/backend-api/conversation/conv-aaa-1"
  );
  const resp = await respPromise;
  const body = await resp.json(); // 页面正常拿到响应
  assert.equal(body.conversation_id, "conv-aaa-1");
  const msg = await waitFor(() =>
    a.messages.find((m) => m.payload && m.payload.source_of === "page"));
  assert.equal(msg.payload.conversations[0].conversation_id, "conv-aaa-1");
});

test("adapter：页面用相对路径请求对话时也能即时采集", async () => {
  // 页面真实调用用的是相对路径
  const routes = standardRoutes();
  const b = loadAdapter({ ...routes, "/backend-api/conversation/conv-aaa-1": routes["https://chatgpt.com/backend-api/conversation/conv-aaa-1"] });
  await (await b.window.fetch("/backend-api/conversation/conv-aaa-1")).json();
  const msg = await waitFor(() => b.messages.find((m) => m.payload && m.payload.source_of === "page"));
  assert.equal(msg.payload.conversations[0].conversation_id, "conv-aaa-1");
});

test("adapter：临时聊天（is_temporary_chat）不即时采集", async () => {
  const routes = standardRoutes();
  routes["https://chatgpt.com/backend-api/conversation/conv-aaa-1"] = async () =>
    jsonResp({ ...CONV_FIXTURE, is_temporary_chat: true });
  const a = loadAdapter(routes);
  await (await a.window.fetch("https://chatgpt.com/backend-api/conversation/conv-aaa-1")).json();
  await new Promise((r) => setTimeout(r, 50));
  assert.ok(!a.messages.some((m) => m.payload && m.payload.source_of === "page"));
});
