"""Starts the Tonebridge server in TEST mode (no internet, no real keys).
Used only by e2e_test.py. The 'premium voice' returns a short test sound instead of calling ElevenLabs."""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
os.environ["TONEBRIDGE_MOCK"] = "1"
os.environ["ELEVENLABS_API_KEY"] = "test-key"
os.environ["TONEBRIDGE_DB"] = os.path.join(tempfile.mkdtemp(), "e2e.db")

import core  # noqa: E402

with open(os.path.join(HERE, "test_voice.mp3"), "rb") as f:
    TEST_MP3 = f.read()
core.elevenlabs_speech = lambda text, emotion: TEST_MP3

import uvicorn  # noqa: E402
import server  # noqa: E402

uvicorn.run(server.app, host="127.0.0.1", port=int(os.environ.get("PORT", "8000")), log_level="warning")
