const DEFAULTS = { enabled: false, lang: "hi", engine: "browser", origVolume: 0.12, showText: true, backendUrl: "http://127.0.0.1:8000" };
const $ = (id) => document.getElementById(id);
const EMOJI = { neutral: "😐 Neutral", angry: "😠 Angry", happy: "😊 Happy", sad: "😢 Sad", excited: "🤩 Excited" };

chrome.storage.sync.get(DEFAULTS, (s) => {
  $("enabled").checked = s.enabled;
  $("lang").value = s.lang;
  $("engine").value = s.engine;
  $("origVolume").value = Math.round(s.origVolume * 100);
  $("volText").textContent = Math.round(s.origVolume * 100) + "%";
  $("showText").checked = s.showText;
  $("backendUrl").value = s.backendUrl;
  refresh();
});

$("enabled").addEventListener("change", (e) => chrome.storage.sync.set({ enabled: e.target.checked }));
$("lang").addEventListener("change", (e) => chrome.storage.sync.set({ lang: e.target.value }));
$("engine").addEventListener("change", (e) => chrome.storage.sync.set({ engine: e.target.value }));
$("showText").addEventListener("change", (e) => chrome.storage.sync.set({ showText: e.target.checked }));
$("backendUrl").addEventListener("change", (e) => { chrome.storage.sync.set({ backendUrl: e.target.value.trim() || DEFAULTS.backendUrl }); checkHealth(); });
$("origVolume").addEventListener("input", (e) => {
  $("volText").textContent = e.target.value + "%";
  chrome.storage.sync.set({ origVolume: Number(e.target.value) / 100 });
});

function show(dotClass, text, sub) {
  $("dot").className = "dot " + dotClass;
  $("statusText").textContent = text;
  $("statusSub").textContent = sub || "";
}

function refresh() {
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    const tab = tabs && tabs[0];
    if (!tab) return show("", "No tab", "");
    chrome.tabs.sendMessage(tab.id, { type: "get-status" }, (st) => {
      if (chrome.runtime.lastError || !st) return show("bad", "Not available on this page", "Open a page with a video (reload it if you just installed Tonebridge).");
      if (!st.enabled) return show("", "Off", "Turn the switch on, then press play.");
      if (st.state === "error") return show("bad", "Problem", st.message);
      if (st.state === "idle") return show("work", "Waiting", st.message || "No video found yet.");
      if (st.state === "loading") return show("work", "Looking for captions…", "");
      if (st.state === "live") return show("ok", "Live mode", st.message || (st.emotion ? EMOJI[st.emotion] : ""));
      const sub = (st.warning ? st.warning + " " : "") + (st.emotion ? "Now: " + EMOJI[st.emotion] : "Press play on the video.");
      show(st.warning ? "work" : "ok", "Dubbing: " + st.dubbed + " of " + st.cues + " lines ready", sub);
    });
  });
}

function checkHealth() {
  chrome.runtime.sendMessage({ type: "health" }, (h) => {
    if (chrome.runtime.lastError || !h || !h.ok) { $("health").textContent = "Server is OFF. Start it with: python server.py"; return; }
    $("health").textContent = "Server is on. AI key: " + (h.ai ? "yes" : "NO") + " · ElevenLabs key: " + (h.elevenlabs ? "yes" : "no");
  });
}

checkHealth();
setInterval(refresh, 1000);
