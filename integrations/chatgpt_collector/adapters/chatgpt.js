// D-6 ChatGPT 平台适配器（MAIN world content script）。
//
// 任务书硬性要求：ChatGPT 内部接口调用全部集中在 adapters/chatgpt.js 一个文件，
// 改版时只改这里。不抓 DOM；在页面自身上下文里用用户已有登录态读取后端 JSON。
//
// 隐私：accessToken 只保存在本文件的内存变量里，绝不写 chrome.storage、
// 绝不发给 Hindsight（本文件根本不 import chrome.* —— MAIN world 也没有）。
//
// 与扩展其余部分的通信只走 window.postMessage（对面是 content/bridge.js）：
//   bridge → adapter：{ source: "hindsight-bridge", type: "crawl" | "get", payload }
//   adapter → bridge：{ source: "hindsight-adapter", type: "conversations" | "error", payload }
// 即时采集：patch window.fetch，捕获页面自身对 /backend-api/conversation/<id>
// 的响应（"复用页面自身的请求结果"，不额外发请求），同样经 bridge 上报。
(function () {
  "use strict";

  if (window.__hindsightChatGPT) return; // 防重复注入

  let cachedAccessToken = null; // 仅内存；进程级缓存，401 时重取

  async function getAccessToken() {
    const r = await fetch("https://chatgpt.com/api/auth/session", {
      credentials: "include",
    });
    if (!r.ok) throw new Error("auth/session " + r.status);
    const data = await r.json();
    const token = data && data.accessToken;
    if (typeof token !== "string" || !token) {
      throw new Error("no accessToken (未登录或会话过期)");
    }
    cachedAccessToken = token;
    return token;
  }

  async function authedFetch(url) {
    const token = cachedAccessToken || (await getAccessToken());
    let r = await fetch(url, {
      credentials: "include",
      headers: { Authorization: "Bearer " + token },
    });
    if (r.status === 401) {
      cachedAccessToken = null;
      return authedFetch(url); // token 失效：重取一次再试
    }
    if (!r.ok) throw new Error(url.split("?")[0] + " → " + r.status);
    return r;
  }

  // 按更新时间排序的最近对话列表（分页由调用方决定 offset/limit）
  async function listRecent(offset, limit) {
    const r = await authedFetch(
      "https://chatgpt.com/backend-api/conversations?offset=" +
        encodeURIComponent(offset || 0) +
        "&limit=" + encodeURIComponent(limit || 28) +
        "&order=updated&is_archived=false"
    );
    const data = await r.json();
    const items = (data && Array.isArray(data.items)) ? data.items : [];
    return items.map((it) => ({
      id: it.id,
      title: it.title,
      update_time: typeof it.update_time === "number" ? it.update_time : null,
      is_temporary: it.is_temporary === true,
    }));
  }

  // 单个对话完整 JSON（结构与官方导出 conversations.json 单条一致）
  async function getConversation(id) {
    const r = await authedFetch(
      "https://chatgpt.com/backend-api/conversation/" + encodeURIComponent(id)
    );
    return r.json();
  }

  // ---- bridge 协议 ----
  async function handleCrawl(payload) {
    // payload: { offset, limit } → 列表（调用方做过滤/节流决策）
    return { conversations: await listRecent(payload && payload.offset, payload && payload.limit) };
  }

  async function handleGet(payload) {
    // payload: { ids: [...] } → 逐个取完整 JSON（不在此处节流；节流由调用方控制）
    const out = [];
    for (const id of payload.ids || []) {
      const conv = await getConversation(id);
      if (conv && !conv.is_temporary) out.push(conv);
    }
    return { conversations: out };
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    const msg = event.data;
    if (!msg || msg.source !== "hindsight-bridge") return;
    (async () => {
      try {
        if (msg.type === "crawl") {
          const result = await handleCrawl(msg.payload);
          post(result, msg.reqId);
        } else if (msg.type === "get") {
          const result = await handleGet(msg.payload);
          post(result, msg.reqId);
        }
      } catch (err) {
        post({ error: String((err && err.message) || err) }, msg.reqId);
      }
    })();
  });

  function post(payload, reqId) {
    window.postMessage(
      { source: "hindsight-adapter", payload, reqId },
      window.location.origin
    );
  }

  // ---- 即时采集：捕获页面自身的对话响应（不额外发请求） ----
  const CONV_RE = /^https:\/\/chatgpt\.com\/backend-api\/conversation\/[a-f0-9-]+/;
  const origFetch = window.fetch;
  window.fetch = async function (...args) {
    const resp = await origFetch.apply(this, args);
    try {
      const url = typeof args[0] === "string" ? args[0] : (args[0] && args[0].url) || "";
      if (CONV_RE.test(url) && resp.ok) {
        const clone = resp.clone();
        clone.json().then((conv) => {
          if (conv && conv.conversation_id && !conv.is_temporary) {
            post({ conversations: [conv], source_of: "page" });
          }
        }).catch(() => {});
      }
    } catch (_) { /* 即时采集失败不影响页面 */ }
    return resp;
  };

  window.__hindsightChatGPT = { listRecent, getConversation, getAccessToken };
})();
