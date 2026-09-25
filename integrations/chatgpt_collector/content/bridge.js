// D-6 桥（isolated world content script）：
// 唯一职责是把 MAIN world 适配器（adapters/chatgpt.js）的消息转给
// service worker（chrome.runtime.sendMessage），并把 SW 的采集指令
// 转成 window.postMessage。不碰任何 ChatGPT 接口，不持久化任何内容。
(function () {
  "use strict";

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    const msg = event.data;
    if (!msg || msg.source !== "hindsight-adapter") return;
    if (msg.reqId) return; // 带 reqId 的是补采请求的回复，由下面的 onAck 处理，不当成即时采集
    // adapter → SW（即时采集走这里）
    try {
      chrome.runtime.sendMessage({ type: "adapter-conversations", payload: msg.payload });
    } catch (_) { /* 扩展重载中的瞬态错误 */ }
  });

  // SW → adapter：转成 window 事件由 adapter 的 message 监听处理
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (!msg || msg.type !== "adapter-request") return false;
    const reqId = msg.reqId;
    function onAck(event) {
      if (event.source !== window) return;
      const data = event.data;
      if (!data || data.source !== "hindsight-adapter" || data.reqId !== reqId) return;
      window.removeEventListener("message", onAck);
      sendResponse(data.payload);
    }
    window.addEventListener("message", onAck);
    window.postMessage(
      { source: "hindsight-bridge", type: msg.adapterType, payload: msg.payload, reqId },
      window.location.origin
    );
    setTimeout(() => {
      window.removeEventListener("message", onAck);
      try { sendResponse({ error: "adapter timeout" }); } catch (_) {}
    }, 30000);
    return true; // 异步 sendResponse
  });
})();
