// D-6 IndexedDB 封装（service worker 内使用）。
// 库：hindsight-collector；store：
//   conversations（keyPath conversation_id）—— { conversation_id, title, update_time,
//     json（完整对话 JSON）, uploaded_update_time|null, fetched_at, retry: {attempts,next_retry_at} }
//   meta（keyPath key）—— { key, value }（waterline / last_sync / last_error）
//   excluded（keyPath conversation_id）
const DB_NAME = "hindsight-collector";
const DB_VERSION = 1;

function openDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains("conversations")) {
        db.createObjectStore("conversations", { keyPath: "conversation_id" });
      }
      if (!db.objectStoreNames.contains("meta")) {
        db.createObjectStore("meta", { keyPath: "key" });
      }
      if (!db.objectStoreNames.contains("excluded")) {
        db.createObjectStore("excluded", { keyPath: "conversation_id" });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function withStore(name, mode, fn) {
  const db = await openDb();
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction(name, mode);
      const store = tx.objectStore(name);
      const result = fn(store);
      tx.oncomplete = () => resolve(result && result.__value !== undefined ? result.__value : result);
      tx.onerror = () => reject(tx.error);
    });
  } finally {
    db.close();
  }
}

function requestToValue(req) {
  const p = new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  p.__value = undefined;
  return p;
}

export async function putConversation(record) {
  const db = await openDb();
  try {
    await new Promise((resolve, reject) => {
      const tx = db.transaction("conversations", "readwrite");
      tx.objectStore("conversations").put(record);
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
    });
  } finally {
    db.close();
  }
}

export async function getConversationRecord(id) {
  const db = await openDb();
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction("conversations", "readonly");
      const req = tx.objectStore("conversations").get(id);
      req.onsuccess = () => resolve(req.result || null);
      req.onerror = () => reject(req.error);
    });
  } finally {
    db.close();
  }
}

export async function allConversations() {
  const db = await openDb();
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction("conversations", "readonly");
      const req = tx.objectStore("conversations").getAll();
      req.onsuccess = () => resolve(req.result || []);
      req.onerror = () => reject(req.error);
    });
  } finally {
    db.close();
  }
}

export async function deleteConversation(id) {
  const db = await openDb();
  try {
    await new Promise((resolve, reject) => {
      const tx = db.transaction("conversations", "readwrite");
      tx.objectStore("conversations").delete(id);
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
    });
  } finally {
    db.close();
  }
}

export async function getMeta(key) {
  const db = await openDb();
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction("meta", "readonly");
      const req = tx.objectStore("meta").get(key);
      req.onsuccess = () => resolve(req.result ? req.result.value : null);
      req.onerror = () => reject(req.error);
    });
  } finally {
    db.close();
  }
}

export async function setMeta(key, value) {
  const db = await openDb();
  try {
    await new Promise((resolve, reject) => {
      const tx = db.transaction("meta", "readwrite");
      tx.objectStore("meta").put({ key, value });
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
    });
  } finally {
    db.close();
  }
}

export async function addExcluded(id) {
  const db = await openDb();
  try {
    await new Promise((resolve, reject) => {
      const tx = db.transaction("excluded", "readwrite");
      tx.objectStore("excluded").put({ conversation_id: id });
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
    });
  } finally {
    db.close();
  }
}

export async function removeExcluded(id) {
  const db = await openDb();
  try {
    await new Promise((resolve, reject) => {
      const tx = db.transaction("excluded", "readwrite");
      tx.objectStore("excluded").delete(id);
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
    });
  } finally {
    db.close();
  }
}

export async function allExcluded() {
  const db = await openDb();
  try {
    return await new Promise((resolve, reject) => {
      const tx = db.transaction("excluded", "readonly");
      const req = tx.objectStore("excluded").getAll();
      req.onsuccess = () => resolve((req.result || []).map((r) => r.conversation_id));
      req.onerror = () => reject(req.error);
    });
  } finally {
    db.close();
  }
}
