"""End-to-end test: loads the real extension into Chromium and plays the demo video.
Needs:  pip install playwright  (and its Chromium).  Run from the tonebridge folder:  python tests/e2e_test.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
EXT = os.path.join(ROOT, "extension")
SHOTS = os.environ.get("SHOTS", tempfile.mkdtemp())
URL = "http://127.0.0.1:8000/demo/"

failures = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (("  -> " + str(detail)) if detail != "" else ""))
    if not ok:
        failures.append(name)


def wait_server():
    for _ in range(50):
        try:
            urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=1)
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("server did not start")


def record(page, seconds):
    """Watches the overlay and records every line that actually gets voiced (the 'spoken' counter goes up)."""
    seen = []
    last_spoken = page.evaluate("+((document.getElementById('tonebridge-overlay')||{dataset:{}}).dataset.spoken||0)")
    end = time.time() + seconds
    while time.time() < end:
        d = page.evaluate("""() => { const o = document.getElementById('tonebridge-overlay');
            return o ? { line:o.dataset.line||'', emotion:o.dataset.emotion||'', engine:o.dataset.engine||'',
                         voice:o.dataset.voice||'', spoken:+(o.dataset.spoken||0), ready:+(o.dataset.ready||0), cues:+(o.dataset.cues||0),
                         vol: document.querySelector('video').volume } : null }""")
        if d and d["spoken"] > last_spoken:
            last_spoken = d["spoken"]
            seen.append(d)
        time.sleep(0.08)
    return seen


def main():
    server = subprocess.Popen([sys.executable, os.path.join(ROOT, "tests", "run_test_server.py")])
    try:
        wait_server()
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                tempfile.mkdtemp(), channel="chromium", headless=True,
                args=["--disable-extensions-except=" + EXT, "--load-extension=" + EXT,
                      "--autoplay-policy=no-user-gesture-required"],
                viewport={"width": 900, "height": 700})
            sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=10000)
            ext_id = sw.url.split("/")[2]
            print("extension loaded, id:", ext_id)

            # ---- 1. free browser voice
            sw.evaluate("chrome.storage.sync.set({enabled:true, lang:'hi', engine:'browser', origVolume:0.12, showText:true, backendUrl:'http://127.0.0.1:8000'})")
            page = ctx.new_page()
            page.goto(URL)
            page.wait_for_selector("video")
            page.wait_for_function("document.getElementById('tonebridge-overlay') !== null", timeout=8000)
            time.sleep(1.5)
            page.evaluate("document.querySelector('video').play()")
            seen = record(page, 27)
            page.screenshot(path=os.path.join(SHOTS, "demo_playing.png"))
            emotions = [s["emotion"] for s in seen]
            print("heard:", [(s["emotion"], s["line"]) for s in seen])
            check("6 lines were voiced in order", emotions == ["angry", "happy", "sad", "excited", "neutral", "angry"], emotions)
            check("lines were sent through the server (mock translation)", all(s["line"].startswith("(hi) ") for s in seen))
            check("free voice used", all(s["engine"] == "browser" for s in seen))
            voices = [json.loads(s["voice"]) for s in seen]
            pitch = {s["emotion"]: v["pitch"] for s, v in zip(seen, voices)}
            check("angry is lower pitched than neutral", pitch.get("angry", 9) < pitch.get("neutral", 0), pitch)
            check("happy and excited are higher than neutral", pitch.get("happy", 0) > 1 and pitch.get("excited", 0) > pitch.get("happy", 9), pitch)
            check("sad is slower than angry", voices[2]["rate"] < voices[0]["rate"], [v["rate"] for v in voices])
            check("original sound lowered", all(abs(s["vol"] - 0.12) < 0.02 for s in seen), [s["vol"] for s in seen])

            # ---- 2. seeking replays the right line
            page.evaluate("const v=document.querySelector('video'); v.currentTime=0.2; v.play()")
            seen2 = record(page, 4)
            check("after seeking back, the first line is voiced again", bool(seen2) and seen2[0]["emotion"] == "angry", [s["line"] for s in seen2])

            # ---- 3. popup
            pop = ctx.new_page()
            pop.set_viewport_size({"width": 360, "height": 520})
            pop.goto("chrome-extension://%s/popup.html" % ext_id)
            time.sleep(1.2)
            health = pop.text_content("#health")
            check("popup knows the server is on", "Server is on" in health, health)
            pop.screenshot(path=os.path.join(SHOTS, "popup.png"))

            # ---- 4. premium voice (test server returns a test sound)
            sw.evaluate("chrome.storage.sync.set({engine:'elevenlabs', lang:'same'})")
            page.bring_to_front()
            page.evaluate("const v=document.querySelector('video'); v.currentTime=4.8; v.play()")
            seen3 = record(page, 5)
            check("premium engine used when audio is available", bool(seen3) and seen3[0]["engine"] == "elevenlabs", [(s["engine"], s["line"]) for s in seen3])
            check("'same language' keeps the original text", bool(seen3) and not seen3[0]["line"].startswith("("), [s["line"] for s in seen3])

            # ---- 4b. Studio: upload a file -> captions -> edit -> download -> dub with the extension
            sw.evaluate("chrome.storage.sync.set({engine:'browser', lang:'hi', enabled:true})")
            studio = ctx.new_page()
            studio.goto("http://127.0.0.1:8000/studio/")
            studio.set_input_files("#file", os.path.join(ROOT, "demo", "sample.mp4"))
            studio.click("#go")
            studio.wait_for_selector("#rows input", timeout=10000)
            rows = studio.locator("#rows input")
            check("Studio made 6 caption lines from the uploaded file", rows.count() == 6, rows.count())
            check("first caption has the right words", rows.nth(0).input_value() == "Where were you all this time?", rows.nth(0).input_value())
            rows.nth(0).fill("Where have you been all this time?")
            studio.click("#apply")
            with studio.expect_download() as dl:
                studio.click("#dl")
            vtt = open(dl.value.path(), encoding="utf-8").read()
            check("downloaded .vtt has times and the edited text", vtt.startswith("WEBVTT") and "00:00:01.000 --> 00:00:04.000" in vtt and "Where have you been" in vtt, vtt[:90].replace("\n", " | "))
            time.sleep(1.5)
            studio.evaluate("document.querySelector('video').play()")
            seen4 = record(studio, 7)
            check("extension dubs the Studio captions (edited text used)", bool(seen4) and "Where have you been all this time?" in seen4[0]["line"], [s["line"] for s in seen4][:2])
            studio.screenshot(path=os.path.join(SHOTS, "studio.png"), full_page=True)
            studio.set_input_files("#file", files=[{"name": "empty.mp3", "mimeType": "audio/mpeg", "buffer": b""}])
            studio.click("#go")
            studio.wait_for_function("document.getElementById('msg').className === 'err'", timeout=8000)
            check("empty file gives a friendly error", "empty" in studio.inner_text("#msg").lower(), studio.inner_text("#msg"))
            r = ctx.request.post("http://127.0.0.1:8000/api/transcribe", headers={"Origin": "https://evil.example"}, multipart={"file": {"name": "a.mp3", "mimeType": "audio/mpeg", "buffer": b"abc"}})
            check("another website cannot use the server", r.status == 403, r.status)
            studio.close()
            page.bring_to_front()

            # ---- 5. turning it off
            sw.evaluate("chrome.storage.sync.set({enabled:false})")
            time.sleep(1.5)
            vol = page.evaluate("document.querySelector('video').volume")
            hidden = page.evaluate("(() => { const o=document.getElementById('tonebridge-overlay'); return !o || getComputedStyle(o).display==='none' })()")
            check("original volume restored when switched off", abs(vol - 1.0) < 0.02, vol)
            check("overlay hidden when switched off", hidden)
            ctx.close()
    finally:
        server.terminate()
    print("\nscreenshots in", SHOTS)
    print("ALL PASSED" if not failures else "FAILED: " + ", ".join(failures))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
