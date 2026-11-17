// Tonebridge background worker.
// Web pages are not allowed to talk to your own computer's server directly,
// so the page sends a message here and this worker makes the call.

const DEFAULT_URL = "http://127.0.0.1:8000";

async function baseUrl() {
  const { backendUrl } = await chrome.storage.sync.get({ backendUrl: DEFAULT_URL });
  const url = String(backendUrl || DEFAULT_URL).replace(/\/+$/, "");
  // Safety: in this MVP the server must be on your own computer.
  if (!/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(url)) return DEFAULT_URL;
  return url;
}

async function callServer(path, options) {
  const base = await baseUrl();
  const res = await fetch(base + path, options);
  let data = {};
  try { data = await res.json(); } catch (e) { /* not JSON */ }
  return { res, data };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "dub") {
    (async () => {
      try {
        const { res, data } = await callServer("/api/dub", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(msg.payload),
        });
        if (!res.ok) sendResponse({ ok: false, error: data.error || "Server error " + res.status });
        else sendResponse({ ok: true, items: data.items || [], warnings: data.warnings || [] });
      } catch (e) {
        sendResponse({ ok: false, error: "Cannot reach the Tonebridge server. Is it running?" });
      }
    })();
    return true; // we answer later
  }
  if (msg && msg.type === "health") {
    (async () => {
      try {
        const { res, data } = await callServer("/api/health", {});
        sendResponse(res.ok ? { ok: true, ...data } : { ok: false });
      } catch (e) {
        sendResponse({ ok: false });
      }
    })();
    return true;
  }
});
