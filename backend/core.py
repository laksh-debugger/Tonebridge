"""Tonebridge core logic.

This file has NO web framework in it, so it is easy to read and to test.
What it does for every subtitle line:
  1. Translates it and decides the emotion (angry / happy / sad / excited / neutral)
     -> uses an AI language model (Claude) if you gave a key, otherwise a simple
        built-in rule-based guess (and no translation).
  2. Optionally turns the line into emotional speech with ElevenLabs (premium).
  3. Saves the result in a small database file, so the same line is never paid for twice.
"""
import base64
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

EMOTIONS = ("neutral", "angry", "happy", "sad", "excited")
LANG_NAMES = {
    "hi": "Hindi", "en": "English", "es": "Spanish", "fr": "French",
    "de": "German", "ja": "Japanese", "same": None,   # "same" = keep the language, only add emotion
}
ENGINES = ("browser", "elevenlabs")
MAX_CUES_PER_REQUEST = 20
MAX_TEXT_LENGTH = 400

# ElevenLabs v3 understands "audio tags" in square brackets, e.g. [angry].
EL_TAGS = {"neutral": "", "angry": "[angry] ", "happy": "[happy] ", "sad": "[sad] ", "excited": "[excited] "}

_db_lock = threading.Lock()


def env(name, default=""):
    return os.environ.get(name, default).strip()


def mock_mode():
    """Used only by the automatic tests: no internet, no keys."""
    return env("TONEBRIDGE_MOCK") == "1"


def db_path():
    return env("TONEBRIDGE_DB") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache.db")


# ---------------------------------------------------------------- saving the work
def _db():
    con = sqlite3.connect(db_path(), timeout=10)
    con.execute("CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, v TEXT)")
    return con


def cache_get(key):
    with _db_lock:
        con = _db()
        try:
            row = con.execute("SELECT v FROM cache WHERE k=?", (key,)).fetchone()
        finally:
            con.close()
    return json.loads(row[0]) if row else None


def cache_set(key, value):
    with _db_lock:
        con = _db()
        try:
            con.execute("INSERT OR REPLACE INTO cache (k, v) VALUES (?, ?)", (key, json.dumps(value)))
            con.commit()
        finally:
            con.close()


def make_key(kind, *parts):
    raw = "|".join(parts)
    return kind + ":" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- emotion (offline guess)
_RULES = [
    ("angry", r"\b(angry|furious|hate|damn|shut up|stop it|how dare|idiot|stupid|get out|liar|enough|unacceptable|where were you)\b"),
    ("sad", r"\b(sorry|miss you|miss him|miss her|cry|crying|tears|lost|alone|goodbye|gone|died|dead|sad|unfortunately|heartbroken|lonely)\b"),
    ("excited", r"\b(wow|amazing|incredible|unbelievable|can't believe|cannot believe|let's go|we did it|awesome|oh my god)\b"),
    ("happy", r"\b(love|great|wonderful|thank you|thanks|glad|happy|haha|yay|beautiful|lovely|congratulations|welcome)\b"),
]


def heuristic_emotion(text):
    """A very simple guess from words and punctuation. Used when there is no AI key."""
    t = text.strip()
    low = t.lower()
    letters = [c for c in t if c.isalpha()]
    shouting = len(letters) >= 4 and sum(c.isupper() for c in letters) / len(letters) > 0.7
    bangs = t.count("!")
    for name, pattern in _RULES:
        if re.search(pattern, low):
            strength = 0.6 + min(0.4, 0.15 * bangs + (0.2 if shouting else 0))
            if name == "happy" and bangs >= 2:
                name = "excited"
            return name, round(min(1.0, strength), 2)
    if shouting and bangs:
        return "angry", 0.8
    if bangs >= 2:
        return "excited", 0.7
    if bangs == 1:
        return "excited", 0.5
    if t.endswith("..."):
        return "sad", 0.4
    return "neutral", 0.5


# ---------------------------------------------------------------- web calls
def _post(url, headers, body, timeout=60):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"content-type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ---------------------------------------------------------------- AI: translate + emotion
SYSTEM_PROMPT = """You are the translation and acting-direction engine of Tonebridge, a tool that dubs videos and keeps the speaker's emotion.
You receive numbered subtitle lines from ONE video, in order. Use the neighbouring lines as context.
For every line return:
- "translation": the natural SPOKEN version in __LANG__. Keep it short enough to be said in about the same time as the original. Keep names. No brackets, no stage directions.
- "emotion": how the speaker feels, exactly one of: neutral, angry, happy, sad, excited.
- "intensity": a number from 0 to 1 (how strongly).
__RULE__
Reply with ONLY a JSON array with one object per line, like:
[{"i": 0, "translation": "...", "emotion": "angry", "intensity": 0.8}]"""


def _extract_json_array(text):
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON array in model reply")
    return json.loads(text[start:end + 1])


def llm_analyze(cues, lang):
    """Ask Claude for translation + emotion. Returns {i: {...}}. Raises on failure."""
    key = env("ANTHROPIC_API_KEY")
    name = LANG_NAMES.get(lang)
    if name:
        system = SYSTEM_PROMPT.replace("__LANG__", name).replace("__RULE__", "")
    else:
        system = SYSTEM_PROMPT.replace("__LANG__", "the original language").replace(
            "__RULE__", 'Do NOT translate. Copy the original text into "translation" unchanged.')
    body = {
        "model": env("LLM_MODEL", "claude-haiku-5-5"),
        "max_tokens": 4000,
        "system": system,
        "messages": [{"role": "user",
                      "content": json.dumps([{"i": c["i"], "text": c["text"]} for c in cues], ensure_ascii=False)}],
    }
    raw = _post("https://api.anthropic.com/v1/messages",
                {"x-api-key": key, "anthropic-version": "2023-06-01"}, body)
    reply = json.loads(raw)
    text = "".join(b.get("text", "") for b in reply.get("content", []) if b.get("type") == "text")
    return _clean_analysis(_extract_json_array(text), cues)


def _clean_analysis(items, cues):
    by_id = {}
    for it in items:
        try:
            by_id[int(it["i"])] = it
        except (KeyError, TypeError, ValueError):
            continue
    out = {}
    for c in cues:
        it = by_id.get(c["i"])
        guess, guess_strength = heuristic_emotion(c["text"])
        if not it:
            out[c["i"]] = {"translation": c["text"], "emotion": guess, "intensity": guess_strength}
            continue
        emotion = str(it.get("emotion", "")).lower()
        if emotion not in EMOTIONS:
            emotion = guess
        try:
            intensity = max(0.0, min(1.0, float(it.get("intensity", 0.7))))
        except (TypeError, ValueError):
            intensity = 0.7
        translation = str(it.get("translation") or c["text"]).strip() or c["text"]
        out[c["i"]] = {"translation": translation, "emotion": emotion, "intensity": round(intensity, 2)}
    return out


def analyze(cues, lang):
    """Returns (results, trustworthy, warnings). Only trustworthy results are saved to the database."""
    if mock_mode():
        out = {}
        for c in cues:
            emo, strength = heuristic_emotion(c["text"])
            text = c["text"] if lang == "same" else "(" + lang + ") " + c["text"]
            out[c["i"]] = {"translation": text, "emotion": emo, "intensity": strength}
        return out, True, []

    if env("ANTHROPIC_API_KEY"):
        try:
            return llm_analyze(cues, lang), True, []
        except Exception as exc:  # network error, bad key, bad reply ...
            warning = "AI step failed (" + type(exc).__name__ + "): using a simple guess instead."
            if isinstance(exc, urllib.error.HTTPError):
                warning = "AI step failed (HTTP %s). Check your ANTHROPIC_API_KEY and credits. Using a simple guess instead." % exc.code
    else:
        warning = ("No ANTHROPIC_API_KEY set. Emotion is a simple guess"
                   + ("" if lang == "same" else " and the text is NOT translated") + ".")
    out = {}
    for c in cues:
        emo, strength = heuristic_emotion(c["text"])
        out[c["i"]] = {"translation": c["text"], "emotion": emo, "intensity": strength}
    return out, False, [warning]


# ---------------------------------------------------------------- voice (premium)
def elevenlabs_speech(text, emotion):
    """Returns mp3 bytes. Raises on failure."""
    voice = env("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
    url = "https://api.elevenlabs.io/v1/text-to-speech/%s?output_format=mp3_44100_64" % voice
    body = {"text": EL_TAGS.get(emotion, "") + text, "model_id": env("ELEVENLABS_MODEL", "eleven_v3")}
    return _post(url, {"xi-api-key": env("ELEVENLABS_API_KEY")}, body, timeout=90)


def _voice_for(result):
    key = make_key("au", env("ELEVENLABS_VOICE_ID"), env("ELEVENLABS_MODEL"), result["emotion"], result["translation"])
    hit = cache_get(key)
    if hit:
        return hit["audio"], None
    try:
        audio = base64.b64encode(elevenlabs_speech(result["translation"], result["emotion"])).decode("ascii")
    except urllib.error.HTTPError as exc:
        return None, "ElevenLabs failed (HTTP %s). Check ELEVENLABS_API_KEY. Using the free browser voice instead." % exc.code
    except Exception as exc:
        return None, "ElevenLabs failed (%s). Using the free browser voice instead." % type(exc).__name__
    cache_set(key, {"audio": audio})
    return audio, None


# ---------------------------------------------------------------- the main function
def validate(body):
    """Checks the request from the extension. Returns (cues, lang, engine)."""
    if not isinstance(body, dict):
        raise ValueError("request must be a JSON object")
    lang = body.get("lang", "same")
    engine = body.get("engine", "browser")
    if lang not in LANG_NAMES:
        raise ValueError("unknown language")
    if engine not in ENGINES:
        raise ValueError("unknown engine")
    raw = body.get("cues")
    if not isinstance(raw, list) or not raw:
        raise ValueError("cues must be a non-empty list")
    if len(raw) > MAX_CUES_PER_REQUEST:
        raise ValueError("too many cues in one request (max %d)" % MAX_CUES_PER_REQUEST)
    cues = []
    for c in raw:
        try:
            i = int(c["i"])
            text = re.sub(r"\s+", " ", str(c["text"])).strip()[:MAX_TEXT_LENGTH]
        except (KeyError, TypeError, ValueError):
            raise ValueError("each cue needs i and text")
        if text:
            cues.append({"i": i, "text": text})
    if not cues:
        raise ValueError("no text in cues")
    return cues, lang, engine


def dub(cues, lang, engine):
    """Takes subtitle lines, returns (items, warnings)."""
    warnings = []
    results = {}
    todo = []
    for c in cues:
        hit = cache_get(make_key("an", lang, c["text"]))
        if hit:
            results[c["i"]] = dict(hit)
        else:
            todo.append(c)

    if todo:
        fresh, trustworthy, warn = analyze(todo, lang)
        warnings += warn
        for c in todo:
            results[c["i"]] = fresh[c["i"]]
            if trustworthy:
                cache_set(make_key("an", lang, c["text"]), fresh[c["i"]])

    audio = {}
    if engine == "elevenlabs":
        if not env("ELEVENLABS_API_KEY"):
            warnings.append("No ELEVENLABS_API_KEY set. Using the free browser voice instead.")
        else:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {i: pool.submit(_voice_for, r) for i, r in results.items()}
                for i, fut in futures.items():
                    data, warn = fut.result()
                    if data:
                        audio[i] = data
                    elif warn and warn not in warnings:
                        warnings.append(warn)

    items = []
    for c in cues:
        r = results[c["i"]]
        item = {"i": c["i"], "translation": r["translation"], "emotion": r["emotion"], "intensity": r["intensity"]}
        if c["i"] in audio:
            item["audio"] = audio[c["i"]]
            item["mime"] = "audio/mpeg"
        items.append(item)
    return items, warnings


# ================================================================ AUDIO -> CAPTIONS
# For a file that has no captions: listen to the speech and write captions with times.
MAX_AUDIO_BYTES = 25 * 1024 * 1024


class TranscribeError(Exception):
    """An error with a message that is safe and useful to show to the user."""


MOCK_SEGMENTS = [
    {"start": 1.0, "end": 4.0, "text": "Where were you all this time?"},
    {"start": 5.0, "end": 8.5, "text": "I'm so happy to see you. Thank you for coming!"},
    {"start": 9.5, "end": 13.0, "text": "I'm so sorry... I miss him so much."},
    {"start": 14.0, "end": 17.5, "text": "Wow! We did it! I can't believe it!"},
    {"start": 18.5, "end": 22.5, "text": "The meeting is at three o'clock in the conference room."},
    {"start": 23.0, "end": 25.5, "text": "GET OUT OF MY HOUSE!"},
]


def group_words(words, max_gap=0.8, max_chars=100, max_seconds=8.0):
    """Turns a list of timed words (ElevenLabs Scribe format) into caption lines.
    A new line starts after a pause, after the end of a sentence, or when the line gets long."""
    segments = []
    cur = None

    def close():
        nonlocal cur
        if cur and cur["text"].strip():
            cur["text"] = re.sub(r"\s+", " ", cur["text"]).strip()
            segments.append({"start": round(cur["start"], 2), "end": round(cur["end"], 2), "text": cur["text"]})
        cur = None

    for w in words:
        kind = w.get("type", "word")
        if kind == "audio_event":
            continue
        text = w.get("text", "")
        if kind == "spacing":
            if cur:
                cur["text"] += text
            continue
        start, end = float(w.get("start", 0)), float(w.get("end", 0))
        if cur and (start - cur["end"] > max_gap or len(cur["text"]) > max_chars or end - cur["start"] > max_seconds):
            close()
        if cur is None:
            cur = {"start": start, "end": end, "text": ""}
        cur["text"] += text
        cur["end"] = end
        # End of a sentence: close the line (only if it is not tiny).
        if re.search(r"[.?!।。！？]$", text) and (len(cur["text"]) >= 18 or cur["end"] - cur["start"] >= 1.2):
            close()
    close()
    return segments


def _post_multipart(url, headers, fields, file_field, filename, data, mime, timeout=300):
    boundary = "----tonebridge" + uuid.uuid4().hex
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename or "audio")
    parts = []
    for k, v in fields.items():
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (boundary, k, v)).encode())
    parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\nContent-Type: %s\r\n\r\n'
                  % (boundary, file_field, safe_name, mime or "application/octet-stream")).encode())
    parts.append(data)
    parts.append(("\r\n--%s--\r\n" % boundary).encode())
    req = urllib.request.Request(url, data=b"".join(parts), method="POST",
                                 headers={"content-type": "multipart/form-data; boundary=" + boundary, **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _transcribe_elevenlabs(data, filename, mime):
    try:
        raw = _post_multipart("https://api.elevenlabs.io/v1/speech-to-text",
                              {"xi-api-key": env("ELEVENLABS_API_KEY")},
                              {"model_id": env("ELEVENLABS_STT_MODEL", "scribe_v1")},
                              "file", filename, data, mime)
    except urllib.error.HTTPError as exc:
        raise TranscribeError("ElevenLabs could not read the file (HTTP %s). Check your ELEVENLABS_API_KEY, "
                              "your credit, and that the file is a normal audio or video file." % exc.code)
    except Exception as exc:
        raise TranscribeError("Could not reach ElevenLabs (%s). Check your internet." % type(exc).__name__)
    reply = json.loads(raw)
    segments = group_words(reply.get("words") or [])
    if not segments and reply.get("text"):
        segments = [{"start": 0.0, "end": 5.0, "text": str(reply["text"]).strip()}]
    return segments, reply.get("language_code")


_whisper_model = None


def _transcribe_local(data, filename):
    global _whisper_model
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise TranscribeError(
            "No speech-to-text engine is ready. Either put ELEVENLABS_API_KEY in your .env file, "
            "or install the free local one with:  pip install faster-whisper")
    if _whisper_model is None:
        _whisper_model = WhisperModel(env("WHISPER_MODEL", "base"), compute_type="int8")
    suffix = os.path.splitext(filename or "")[1][:8] or ".audio"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        path = tmp.name
    try:
        segs, info = _whisper_model.transcribe(path, vad_filter=True)
        out = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in segs if s.text.strip()]
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return out, getattr(info, "language", None)


def transcribe(data, filename="audio", mime=""):
    """Takes the bytes of an audio/video file. Returns (segments, language, warnings).
    Each segment is {"start": seconds, "end": seconds, "text": "..."}."""
    if not data:
        raise TranscribeError("The file is empty.")
    if len(data) > MAX_AUDIO_BYTES:
        raise TranscribeError("The file is too big (%d MB). The limit is %d MB." % (len(data) // 1048576, MAX_AUDIO_BYTES // 1048576))
    if mock_mode():
        return [dict(s) for s in MOCK_SEGMENTS], "en", []

    key = make_key("tr", hashlib.sha256(data).hexdigest())
    hit = cache_get(key)
    if hit:
        return hit["segments"], hit.get("language"), []

    engine = env("TRANSCRIBE_ENGINE") or ("elevenlabs" if env("ELEVENLABS_API_KEY") else "local")
    if engine == "elevenlabs":
        if not env("ELEVENLABS_API_KEY"):
            raise TranscribeError("TRANSCRIBE_ENGINE is elevenlabs but ELEVENLABS_API_KEY is empty.")
        segments, language = _transcribe_elevenlabs(data, filename, mime)
    else:
        segments, language = _transcribe_local(data, filename)
    if not segments:
        raise TranscribeError("No speech was found in this file.")
    cache_set(key, {"segments": segments, "language": language})
    return segments, language, []
