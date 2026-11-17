// Tonebridge content script. Runs on every web page.
// Job: find the video, read its captions, ask the server for emotion + translation,
// then speak each line (with the right feeling) at the right moment of the video.
(() => {
  if (window.__tonebridgeLoaded) return;
  window.__tonebridgeLoaded = true;

  // ---------------------------------------------------------------- settings
  const DEFAULTS = { enabled: false, lang: "hi", engine: "browser", origVolume: 0.12, showText: true };
  const LANG_TAG = { hi: "hi-IN", en: "en-US", es: "es-ES", fr: "fr-FR", de: "de-DE", ja: "ja-JP" };

  // How each emotion changes the free browser voice: speed, pitch, loudness.
  const EMO = {
    neutral: { rate: 1.0, pitch: 1.0, volume: 0.9, emoji: "😐", label: "Neutral" },
    angry:   { rate: 1.15, pitch: 0.8, volume: 1.0, emoji: "😠", label: "Angry" },
    happy:   { rate: 1.08, pitch: 1.35, volume: 1.0, emoji: "😊", label: "Happy" },
    sad:     { rate: 0.82, pitch: 0.75, volume: 0.7, emoji: "😢", label: "Sad" },
    excited: { rate: 1.25, pitch: 1.5, volume: 1.0, emoji: "🤩", label: "Excited" },
  };
  const LOOKAHEAD = 40; // seconds of video we dub in advance
  const BATCH = 8;      // lines per server request

  let settings = { ...DEFAULTS };

  // ---------------------------------------------------------------- state
  let gen = 0;                 // changes whenever we must forget everything
  let video = null, sessionKey = "", lastLoadAt = 0;
  let cues = [];
  const results = new Map();   // cue number -> {translation, emotion, intensity, audio?}
  const pending = new Set();
  const failedUntil = new Map();
  let inflight = 0;
  let live = false, liveTimer = null, liveLast = "";
  let status = { state: "idle", message: "" };
  let warning = "";
  let playedIdx = -1, shownIdx = -1, lastEmotion = null, srcLang = null, spokenCount = 0;
  let audioEl = null, savedVolume = null, overlay = null;
  let listeners = [];

  const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const isYouTube = () => location.hostname.endsWith("youtube.com");
  const adShowing = () => !!document.querySelector(".html5-video-player.ad-showing");

  function setStatus(state, message) { status = { state, message: message || "" }; }

  // ---------------------------------------------------------------- settings changes
  chrome.storage.sync.get(DEFAULTS, (s) => { settings = { ...DEFAULTS, ...s }; tick(); });
  chrome.storage.onChanged.addListener((changes) => {
    let reset = false;
    for (const k in changes) {
      const v = changes[k].newValue;
      settings[k] = v === undefined && k in DEFAULTS ? DEFAULTS[k] : v;
      if (k === "lang" || k === "engine") reset = true;
    }
    if (reset) resetResults();
    tick();
  });

  function resetResults() {
    gen++;
    results.clear(); pending.clear(); failedUntil.clear();
    inflight = 0; playedIdx = -1; shownIdx = -1;
    stopVoice();
  }

  // ---------------------------------------------------------------- finding the video
  function pickVideo() {
    let best = null, bestArea = 0;
    for (const v of document.querySelectorAll("video")) {
      const r = v.getBoundingClientRect();
      const area = r.width * r.height;
      if (area > 120 * 80 && area > bestArea && (v.readyState > 0 || v.currentSrc || v.src)) { best = v; bestArea = area; }
    }
    return best;
  }

  // ---------------------------------------------------------------- reading captions
  function askPage(type, timeout = 1500) {
    return new Promise((resolve) => {
      const id = Math.random().toString(36).slice(2);
      const onMsg = (e) => {
        if (e.source === window && e.data && e.data.tb === type + ":reply" && e.data.id === id) {
          window.removeEventListener("message", onMsg); clearTimeout(timer); resolve(e.data.payload);
        }
      };
      const timer = setTimeout(() => { window.removeEventListener("message", onMsg); resolve(null); }, timeout);
      window.addEventListener("message", onMsg);
      window.postMessage({ tb: type, id }, "*");
    });
  }

  function cleanText(html) {
    const doc = new DOMParser().parseFromString(String(html), "text/html");
    return (doc.body.textContent || "").replace(/\s+/g, " ").trim();
  }

  async function loadYouTubeCues() {
    const info = await askPage("TB_TRACKS");
    if (!info || !info.tracks || !info.tracks.length) return null;
    const track = info.tracks.find((t) => t.kind !== "asr") || info.tracks[0];
    srcLang = track.languageCode;
    const url = track.baseUrl + (track.baseUrl.includes("?") ? "&" : "?") + "fmt=json3";
    const res = await fetch(url, { credentials: "include" });
    if (!res.ok) return null;
    const body = await res.text();
    if (!body) return null;
    const out = [];
    for (const ev of JSON.parse(body).events || []) {
      if (!ev.segs) continue;
      const text = ev.segs.map((s) => s.utf8 || "").join("").replace(/\s+/g, " ").trim();
      if (!text) continue;
      const start = (ev.tStartMs || 0) / 1000;
      out.push({ start, end: start + (ev.dDurationMs || 2000) / 1000, text });
    }
    return out;
  }

  async function loadTextTrackCues(v) {
    const tracks = [...v.textTracks].filter((t) => t.kind === "subtitles" || t.kind === "captions");
    if (!tracks.length) return null;
    const pick = tracks.find((t) => t.mode === "showing") || tracks.find((t) => t.default) || tracks[0];
    if (pick.mode === "disabled") pick.mode = "hidden"; // "hidden" = loaded but not drawn
    for (let n = 0; n < 40 && (!pick.cues || !pick.cues.length); n++) await sleep(100);
    if (!pick.cues || !pick.cues.length) return null;
    srcLang = pick.language || null;
    return [...pick.cues].map((c) => ({ start: c.startTime, end: c.endTime, text: cleanText(c.text) })).filter((c) => c.text);
  }

  function prepareCues(list) {
    list = list.filter((c) => c.text && isFinite(c.start) && isFinite(c.end)).sort((a, b) => a.start - b.start);
    const out = [];
    for (const c of list) {
      const prev = out[out.length - 1];
      if (prev && prev.text === c.text && c.start <= prev.end + 0.5) { prev.end = Math.max(prev.end, c.end); continue; }
      out.push({ start: c.start, end: c.end, text: c.text });
    }
    // Two lines must never be spoken at the same time.
    for (let k = 0; k < out.length - 1; k++) {
      if (out[k].end > out[k + 1].start) out[k].end = Math.max(out[k].start + 0.4, out[k + 1].start);
    }
    out.forEach((c, k) => { c.i = k; });
    return out;
  }

  // Fallback for YouTube when the caption file cannot be fetched: read the captions
  // that are drawn on screen (the viewer must switch captions on with the CC button).
  function startLive() {
    live = true;
    setStatus("live", "Reading the on-screen captions. Turn captions on (CC button).");
    liveLast = "";
    liveTimer = setInterval(() => {
      if (!video) return;
      const text = [...document.querySelectorAll(".ytp-caption-segment")].map((n) => n.textContent).join(" ").replace(/\s+/g, " ").trim();
      if (!text || text === liveLast) return;
      liveLast = text;
      const t = video.currentTime;
      const prev = cues[cues.length - 1];
      if (prev && prev.end > t) prev.end = t;
      const cue = { i: cues.length, start: t, end: t + 6, text };
      cues.push(cue);
      requestBatch([cue]);
    }, 400);
  }

  // ---------------------------------------------------------------- session
  function startSession(v, key) {
    video = v; sessionKey = key; lastLoadAt = Date.now();
    const myGen = ++gen;
    cues = []; results.clear(); pending.clear(); failedUntil.clear();
    inflight = 0; live = false; playedIdx = -1; shownIdx = -1; warning = ""; lastEmotion = null;
    setStatus("loading", "Looking for captions…");

    const on = (name, fn) => { v.addEventListener(name, fn); listeners.push([name, fn]); };
    on("seeking", () => { stopVoice(); playedIdx = -1; shownIdx = -1; setOverlayText(null); });
    on("pause", () => { stopVoice(); playedIdx = -1; });
    applyDuck();

    (async () => {
      let found = null;
      try { found = isYouTube() ? await loadYouTubeCues() : null; } catch (e) { found = null; }
      if (myGen !== gen) return;
      if (!found || !found.length) { try { found = await loadTextTrackCues(v); } catch (e) { found = null; } }
      if (myGen !== gen) return;
      if (found && found.length) {
        cues = prepareCues(found);
        setStatus("ready", "");
      } else if (isYouTube()) {
        startLive();
      } else {
        setStatus("error", "No captions found on this video. Tonebridge needs a video with captions (subtitles).");
      }
    })();
  }

  function stopSession() {
    gen++;
    stopVoice();
    if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
    if (video) for (const [name, fn] of listeners) video.removeEventListener(name, fn);
    listeners = [];
    releaseDuck();
    video = null; sessionKey = ""; cues = []; live = false;
    results.clear(); pending.clear(); failedUntil.clear(); inflight = 0;
    playedIdx = -1; shownIdx = -1;
    hideOverlay();
    setStatus("idle", "");
  }

  function tick() {
    if (!settings.enabled) { if (video) stopSession(); return; }
    const v = pickVideo();
    if (!v) {
      if (video) stopSession();
      setStatus("idle", "No video found on this page.");
      return;
    }
    const key = isYouTube() ? location.href : location.href + "|" + (v.currentSrc || "");
    // No captions yet? Try again every few seconds (some sites add them late).
    if (video && !cues.length && !live && status.state === "error" && Date.now() - lastLoadAt > 5000) sessionKey = "";
    if (v !== video || key !== sessionKey) { if (video) stopSession(); startSession(v, key); }
    applyDuck();
    pump();
  }

  // ---------------------------------------------------------------- asking the server
  function pump() {
    if (!video || live || inflight >= 2 || !cues.length) return;
    const t = video.currentTime, now = Date.now();
    const want = [];
    for (const c of cues) {
      if (c.end < t - 0.5) continue;
      if (c.start > t + LOOKAHEAD) break;
      if (results.has(c.i) || pending.has(c.i) || (failedUntil.get(c.i) || 0) > now) continue;
      want.push(c);
      if (want.length >= BATCH) break;
    }
    if (want.length) requestBatch(want);
  }

  function requestBatch(batch) {
    const myGen = gen;
    inflight++;
    batch.forEach((c) => pending.add(c.i));
    chrome.runtime.sendMessage(
      { type: "dub", payload: { lang: settings.lang, engine: settings.engine,
        cues: batch.map((c) => ({ i: c.i, start: c.start, end: c.end, text: c.text })) } },
      (resp) => {
        const err = chrome.runtime.lastError;
        if (myGen !== gen) return;
        inflight = Math.max(0, inflight - 1);
        batch.forEach((c) => pending.delete(c.i));
        if (err || !resp || !resp.ok) {
          setStatus("error", (resp && resp.error) || "Cannot reach the Tonebridge server. Is it running?");
          batch.forEach((c) => failedUntil.set(c.i, Date.now() + 8000));
          return;
        }
        for (const it of resp.items) results.set(it.i, it);
        warning = resp.warnings && resp.warnings.length ? resp.warnings[0] : "";
        setStatus(live ? "live" : "ready", "");
      }
    );
  }

  // ---------------------------------------------------------------- playing the dub
  function cueAt(t) {
    let lo = 0, hi = cues.length - 1, best = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (cues[mid].start <= t + 0.05) { best = mid; lo = mid + 1; } else hi = mid - 1;
    }
    if (best < 0) return null;
    return t < cues[best].end ? cues[best] : null;
  }

  function playLoop() {
    if (!settings.enabled || !video) { hideOverlay(); return; }
    updateOverlay();
    if (video.paused || video.ended || adShowing()) return;
    const t = video.currentTime;
    const c = cueAt(t);
    if (!c) {
      if (shownIdx !== -1) { shownIdx = -1; setOverlayText(null); }
      return;
    }
    if (c.i === playedIdx) return;
    const res = results.get(c.i);
    if (!res || t > c.end - 0.15) return;
    playedIdx = c.i;
    speak(c, res);
  }
  setInterval(playLoop, 120);

  function stopVoice() {
    try { window.speechSynthesis.cancel(); } catch (e) { /* ignore */ }
    if (audioEl) { try { audioEl.pause(); } catch (e) { /* ignore */ } audioEl = null; }
  }

  function langTag() {
    if (settings.lang !== "same") return LANG_TAG[settings.lang] || "en-US";
    const l = String(srcLang || document.documentElement.lang || "en").toLowerCase().slice(0, 2);
    return LANG_TAG[l] || l;
  }

  function pickVoice(tag) {
    const voices = window.speechSynthesis.getVoices();
    if (!voices.length) return null;
    const want = tag.toLowerCase(), base = want.slice(0, 2);
    const norm = (v) => v.lang.replace("_", "-").toLowerCase();
    const sameLang = voices.filter((v) => norm(v).startsWith(base));
    return sameLang.find((v) => norm(v) === want && /google|natural|online/i.test(v.name))
      || sameLang.find((v) => norm(v) === want) || sameLang[0] || null;
  }

  function speak(c, res) {
    stopVoice();
    shownIdx = c.i;
    lastEmotion = res.emotion;
    const dur = Math.max(0.6, c.end - c.start);
    let used = "browser";
    if (settings.engine === "elevenlabs" && res.audio) { playAudio(res, dur); used = "elevenlabs"; }
    else speakBrowser(res, dur);
    setOverlayText(res, used);
  }

  function speakBrowser(res, dur) {
    const e = EMO[res.emotion] || EMO.neutral;
    const strength = 0.5 + 0.5 * clamp(res.intensity == null ? 0.7 : res.intensity, 0, 1);
    const u = new SpeechSynthesisUtterance(res.translation);
    const tag = langTag();
    u.lang = tag;
    const voice = pickVoice(tag);
    if (voice) u.voice = voice;
    let rate = 1 + (e.rate - 1) * strength;
    const needed = (res.translation.length / 14) / dur * 0.95; // speed needed to finish before the next line
    rate = clamp(Math.max(rate, needed), 0.7, 1.8);
    u.rate = rate;
    u.pitch = clamp(1 + (e.pitch - 1) * strength, 0.1, 2);
    u.volume = e.volume;
    spokenCount++;
    lastVoice = { rate: +u.rate.toFixed(2), pitch: +u.pitch.toFixed(2), volume: u.volume };
    window.speechSynthesis.speak(u);
  }
  let lastVoice = null;

  function playAudio(res, dur) {
    const a = new Audio("data:" + (res.mime || "audio/mpeg") + ";base64," + res.audio);
    a.addEventListener("loadedmetadata", () => {
      if (a.duration > dur) a.playbackRate = clamp(a.duration / dur, 1, 1.5);
    });
    a.play().catch(() => speakBrowser(res, dur)); // if the browser blocks it, use the free voice
    audioEl = a;
    spokenCount++;
    lastVoice = { audio: true };
  }

  // ---------------------------------------------------------------- original sound
  function applyDuck() {
    if (!video || !settings.enabled) return;
    if (savedVolume === null) savedVolume = video.volume;
    if (Math.abs(video.volume - settings.origVolume) > 0.01) video.volume = clamp(settings.origVolume, 0, 1);
  }
  function releaseDuck() {
    if (video && savedVolume !== null) video.volume = savedVolume;
    savedVolume = null;
  }
  window.addEventListener("pagehide", releaseDuck);

  // ---------------------------------------------------------------- overlay (shows what Tonebridge is doing)
  function ensureOverlay() {
    const fs = document.fullscreenElement;
    const host = fs && fs.tagName !== "VIDEO" ? fs : document.body;
    if (!overlay) {
      overlay = document.createElement("div");
      overlay.id = "tonebridge-overlay";
      overlay.innerHTML = '<div class="tb-chip"><span class="tb-logo">Tone<b>bridge</b></span><span class="tb-emo"></span></div><div class="tb-text"></div>';
    }
    if (overlay.parentElement !== host) host.appendChild(overlay);
    return overlay;
  }

  function updateOverlay() {
    const o = ensureOverlay();
    const r = video.getBoundingClientRect();
    if (r.width < 50 || r.height < 50) { o.style.display = "none"; return; }
    o.style.display = "block";
    o.style.left = r.left + "px"; o.style.top = r.top + "px";
    o.style.width = r.width + "px"; o.style.height = r.height + "px";
    o.dataset.spoken = String(spokenCount);
    o.dataset.cues = String(cues.length);
    o.dataset.ready = String(results.size);
  }

  function setOverlayText(res, used) {
    if (!overlay) return;
    const emo = overlay.querySelector(".tb-emo"), txt = overlay.querySelector(".tb-text");
    if (!res) { txt.textContent = ""; txt.style.display = "none"; return; }
    const e = EMO[res.emotion] || EMO.neutral;
    emo.textContent = e.emoji + " " + e.label;
    overlay.dataset.emotion = res.emotion;
    overlay.dataset.engine = used || "";
    overlay.dataset.voice = JSON.stringify(lastVoice || {});
    overlay.dataset.line = res.translation;
    txt.textContent = res.translation;
    txt.style.display = settings.showText ? "block" : "none";
  }

  function hideOverlay() { if (overlay) overlay.style.display = "none"; }

  // ---------------------------------------------------------------- talking to the popup
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg && msg.type === "get-status") {
      sendResponse({
        state: status.state, message: status.message, warning,
        cues: cues.length, dubbed: results.size, emotion: lastEmotion,
        hasVideo: !!video, live, enabled: !!settings.enabled,
      });
    }
  });

  setInterval(tick, 1000);
})();
