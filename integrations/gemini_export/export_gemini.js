// 在已登录的 gemini.google.com 页面里运行：依次打开侧边栏每个对话，提取问答文本，
// 每 20 个对话下载一个分段文件 gemini-export-p<时间戳>.json。只读页面 DOM，不调用私有接口、
// 不改任何对话。进度在 window.__gx（只含计数）；已完成的会话 ID 存 localStorage，
// 中断后重新运行会自动跳过。运行期间标签页须保持在前台（见 README）。
(() => {
  if (window.__gx && window.__gx.running) return "already running";
  const KEY = "__gx_done_ids";
  const PART_SIZE = 20;
  const done = new Set(JSON.parse(localStorage.getItem(KEY) || "[]"));
  const saveDone = () => { try { localStorage.setItem(KEY, JSON.stringify([...done])); } catch {} };
  const gx = (window.__gx = { running: true, phase: "list", total: 0, done: 0, failed: 0, turns: 0, parts: 0, error: null });
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const convIdOf = (href) => (href.match(/\/app\/([0-9a-z_]+)/i) || [])[1];

  function sidebarLinks() {
    const seen = new Map();
    for (const a of document.querySelectorAll('a[href*="/app/"]')) {
      const id = convIdOf(a.getAttribute("href") || "");
      if (id && !seen.has(id) && a.isConnected) {
        seen.set(id, { id, title: (a.textContent || "").trim().split("\n")[0].slice(0, 200), a });
      }
    }
    return seen;
  }
  // 窄窗口下 Gemini 会自动收起侧边栏；链接少于 5 个就重新打开
  async function ensureSidebar() {
    if (document.querySelectorAll('a[href*="/app/"]').length < 5) {
      const b = document.querySelector('button[aria-label="打开边栏"]');
      if (b) { b.click(); await sleep(1500); }
    }
  }
  const keeper = setInterval(() => { ensureSidebar(); }, 700);
  const sideScroller = () => [...document.querySelectorAll("infinite-scroller")].find((s) => s.querySelector('a[href*="/app/"]'));
  async function loadWholeSidebar() {
    await ensureSidebar();
    let last = -1, stable = 0;
    while (stable < 6) {
      const sc = sideScroller(); if (sc) sc.scrollTop = sc.scrollHeight;
      await sleep(1500);
      const n = sidebarLinks().size; stable = n === last ? stable + 1 : 0; last = n;
    }
  }
  // 屏幕外的轮次被跳过渲染（content-visibility），innerText 为空；用 textContent，块级元素后补换行
  function blockText(el) {
    const c = el.cloneNode(true);
    c.querySelectorAll("p, li, pre, h1, h2, h3, h4, h5, h6, tr, br, blockquote, .query-text-line").forEach((b) => b.append("\n"));
    return c.textContent.replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
  }
  function readTurns() {
    const turns = [];
    for (const n of document.querySelectorAll("user-query, model-response")) {
      const isUser = n.tagName.toLowerCase() === "user-query";
      const el = isUser ? n.querySelector(".query-text") || n : n.querySelector("message-content") || n;
      const text = blockText(el);
      if (text) turns.push({ role: isUser ? "user" : "assistant", text });
    }
    return turns;
  }
  // 长对话向上懒加载更早的轮次：滚到顶直到轮次数稳定
  async function loadWholeChat() {
    let last = -1, stable = 0;
    for (let i = 0; i < 80 && stable < 2; i++) {
      const first = document.querySelector("user-query, model-response");
      if (first) first.scrollIntoView({ block: "start" });
      const sc = document.querySelector("#chat-history, infinite-scroller.chat-history"); if (sc) sc.scrollTop = 0;
      await sleep(1000);
      const n = document.querySelectorAll("user-query, model-response").length;
      stable = n === last && n > 0 ? stable + 1 : 0; last = n;
    }
  }
  // 只点仍在页面上的链接（点已脱离 DOM 的链接会触发整页跳转，脚本随之丢失）
  async function openChat(id) {
    await ensureSidebar();
    let link = sidebarLinks().get(id);
    for (let i = 0; i < 30 && !link; i++) {
      await ensureSidebar(); const sc = sideScroller(); if (sc) sc.scrollTop = sc.scrollHeight;
      await sleep(1000); link = sidebarLinks().get(id);
    }
    if (!link) throw new Error("link not found");
    link.a.scrollIntoView({ block: "center" }); link.a.click();
    for (let i = 0; i < 60 && !location.pathname.includes(id); i++) await sleep(250);
    if (!location.pathname.includes(id)) throw new Error("nav failed");
  }
  function download(convs) {
    gx.parts++;
    const blob = new Blob([JSON.stringify({ exported_at: new Date().toISOString(), conversations: convs })], { type: "application/json" });
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = `gemini-export-p${Date.now()}.json`;
    document.body.appendChild(a); a.click(); a.remove();
  }

  (async () => {
    let buf = [];
    try {
      await loadWholeSidebar();
      const list = [...sidebarLinks().values()].filter((x) => !done.has(x.id)).map((x) => ({ id: x.id, title: x.title }));
      gx.total = list.length; gx.phase = "chats";
      for (const item of list) {
        try {
          await openChat(item.id); await sleep(1500); await loadWholeChat();
          const turns = readTurns();
          if (turns.length) {
            buf.push({ id: item.id, title: item.title, created_at: null, turns });
            gx.turns += turns.length; done.add(item.id); saveDone();
          } else gx.failed++;
        } catch (e) { gx.failed++; gx.lastErr = String(e).slice(0, 80); }
        gx.done++;
        if (buf.length >= PART_SIZE) { download(buf); buf = []; }
        await sleep(800); // 节流
      }
      gx.phase = "finished";
    } catch (e) { gx.error = String(e).slice(0, 200); gx.phase = "error"; }
    finally {
      if (buf.length) download(buf);
      clearInterval(keeper);
      gx.running = false;
    }
  })();
  return "started";
})();
