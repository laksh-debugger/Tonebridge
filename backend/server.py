"""Tonebridge server. Start it with:  python server.py

It has two jobs:
  POST /api/dub    -> receives subtitle lines from the extension, returns emotion + translation (+ premium voice)
  GET  /api/health -> tells the extension which keys are set
  POST /api/transcribe -> receives an audio/video file, returns captions with times
It also serves the demo page (/demo/) and the Studio page (/studio/).

(Starlette is the engine that FastAPI is built on. Same idea, a few lines less.)
"""
import os

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import core

HERE = os.path.dirname(os.path.abspath(__file__))


def load_env(path):
    """Reads KEY=value lines from a .env file (so you do not need any extra package)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


LOCAL_HOSTS = ("127.0.0.1", "localhost")


def _allowed(request):
    """Only the Tonebridge extension, the Tonebridge pages served by this server, or a command-line
    tool (no Origin) may use the API. This stops a random website from spending your API credits."""
    origin = request.headers.get("origin")
    if origin is None or origin.startswith("chrome-extension://"):
        return True
    host = request.headers.get("host", "")
    return host.split(":")[0] in LOCAL_HOSTS and origin == "%s://%s" % (request.url.scheme, host)


async def health(request):
    if not _allowed(request):
        return JSONResponse({"error": "not allowed"}, status_code=403)
    return JSONResponse({
        "ok": True,
        "ai": bool(core.env("ANTHROPIC_API_KEY")) or core.mock_mode(),
        "elevenlabs": bool(core.env("ELEVENLABS_API_KEY")),
        "mock": core.mock_mode(),
    })


async def dub(request):
    if not _allowed(request):
        return JSONResponse({"error": "not allowed"}, status_code=403)
    try:
        body = await request.json()
        cues, lang, engine = core.validate(body)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception:
        return JSONResponse({"error": "bad request"}, status_code=400)
    items, warnings = await run_in_threadpool(core.dub, cues, lang, engine)
    return JSONResponse({"items": items, "warnings": warnings})


async def transcribe(request):
    """Audio/video file in -> captions with times out."""
    if not _allowed(request):
        return JSONResponse({"error": "not allowed"}, status_code=403)
    try:
        if int(request.headers.get("content-length", "0")) > core.MAX_AUDIO_BYTES + 1048576:
            return JSONResponse({"error": "The file is too big. The limit is %d MB." % (core.MAX_AUDIO_BYTES // 1048576)}, status_code=413)
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            return JSONResponse({"error": "Please choose a file."}, status_code=400)
        data = await upload.read()
        filename, mime = upload.filename or "audio", upload.content_type or ""
    except Exception:
        return JSONResponse({"error": "Could not read the upload."}, status_code=400)
    try:
        segments, language, warnings = await run_in_threadpool(core.transcribe, data, filename, mime)
    except core.TranscribeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    return JSONResponse({"segments": segments, "language": language, "warnings": warnings})


routes = [
    Route("/api/health", health),
    Route("/api/dub", dub, methods=["POST"]),
    Route("/api/transcribe", transcribe, methods=["POST"]),
]
demo_dir = os.path.join(HERE, "..", "demo")
if os.path.isdir(demo_dir):
    routes.append(Mount("/demo", app=StaticFiles(directory=demo_dir, html=True), name="demo"))
studio_dir = os.path.join(HERE, "..", "studio")
if os.path.isdir(studio_dir):
    routes.append(Mount("/studio", app=StaticFiles(directory=studio_dir, html=True), name="studio"))

app = Starlette(routes=routes)

if __name__ == "__main__":
    load_env(os.path.join(HERE, ".env"))
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    print("\nTonebridge server running.")
    print("  AI key (translation + emotion):", "yes" if core.env("ANTHROPIC_API_KEY") else "NO (simple guess only)")
    print("  ElevenLabs key (premium voice):", "yes" if core.env("ELEVENLABS_API_KEY") else "no")
    print("  Demo page:   http://127.0.0.1:%d/demo/" % port)
    print("  Studio page: http://127.0.0.1:%d/studio/   (make captions from an audio or video file)\n" % port)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
