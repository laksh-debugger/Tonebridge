"""Run with:  python -m unittest discover -s tests -v   (from the tonebridge folder)"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
os.environ["TONEBRIDGE_DB"] = os.path.join(tempfile.mkdtemp(), "test_cache.db")
for name in ("ANTHROPIC_API_KEY", "ELEVENLABS_API_KEY", "TONEBRIDGE_MOCK"):
    os.environ.pop(name, None)

import core  # noqa: E402


def fake_claude_reply(items):
    return json.dumps({"content": [{"type": "text", "text": "Here you go:\n" + json.dumps(items)}]}).encode()


class EmotionGuess(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(core.heuristic_emotion("Where were you all this time?")[0], "angry")
        self.assertEqual(core.heuristic_emotion("I'm so sorry, I miss him.")[0], "sad")
        self.assertEqual(core.heuristic_emotion("Wow! We did it!")[0], "excited")
        self.assertEqual(core.heuristic_emotion("Thank you, I love it.")[0], "happy")
        self.assertEqual(core.heuristic_emotion("The meeting is at three.")[0], "neutral")
        self.assertEqual(core.heuristic_emotion("GET OUT OF MY HOUSE!")[0], "angry")


class Validation(unittest.TestCase):
    def test_good_request(self):
        cues, lang, engine = core.validate({"lang": "hi", "engine": "browser", "cues": [{"i": 1, "text": " hello   world "}]})
        self.assertEqual(cues, [{"i": 1, "text": "hello world"}])
        self.assertEqual((lang, engine), ("hi", "browser"))

    def test_bad_requests(self):
        for bad in ({}, {"cues": []}, {"lang": "xx", "cues": [{"i": 1, "text": "a"}]},
                    {"engine": "x", "cues": [{"i": 1, "text": "a"}]},
                    {"cues": [{"i": i, "text": "a"} for i in range(21)]}, {"cues": [{"text": "a"}]}):
            with self.assertRaises(ValueError):
                core.validate(bad)


class WithoutKeys(unittest.TestCase):
    def test_falls_back_and_warns(self):
        line = "Where were you, my friend, for so long?"
        items, warnings = core.dub([{"i": 0, "text": line}], "hi", "browser")
        self.assertEqual(items[0]["emotion"], "angry")
        self.assertEqual(items[0]["translation"], line)  # not translated
        self.assertTrue(any("ANTHROPIC_API_KEY" in w for w in warnings))

    def test_fallback_not_saved_to_database(self):
        core.dub([{"i": 0, "text": "Unsaved line one"}], "hi", "browser")
        self.assertIsNone(core.cache_get(core.make_key("an", "hi", "Unsaved line one")))

    def test_premium_without_key_warns(self):
        _, warnings = core.dub([{"i": 0, "text": "Hello"}], "same", "elevenlabs")
        self.assertTrue(any("ELEVENLABS_API_KEY" in w for w in warnings))


class WithClaude(unittest.TestCase):
    def setUp(self):
        os.environ["ANTHROPIC_API_KEY"] = "test-key"

    def tearDown(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_parses_reply_and_saves_once(self):
        reply = fake_claude_reply([
            {"i": 5, "translation": "तुम इतने समय कहाँ थे?", "emotion": "angry", "intensity": 0.9},
            {"i": 6, "translation": "मुझे बहुत खुशी है!", "emotion": "HAPPY", "intensity": 2},
        ])
        cues = [{"i": 5, "text": "Where were you all this time?"}, {"i": 6, "text": "I am so glad!"}]
        with mock.patch.object(core, "_post", return_value=reply) as post:
            items, warnings = core.dub(cues, "hi", "browser")
            self.assertEqual(warnings, [])
            self.assertEqual(items[0]["translation"], "तुम इतने समय कहाँ थे?")
            self.assertEqual(items[1]["emotion"], "happy")
            self.assertEqual(items[1]["intensity"], 1.0)  # clamped
            self.assertEqual(post.call_count, 1)
            items2, _ = core.dub(cues, "hi", "browser")   # second time: from the database
            self.assertEqual(post.call_count, 1)
            self.assertEqual(items2[0]["translation"], items[0]["translation"])

    def test_missing_and_invalid_items_are_repaired(self):
        reply = fake_claude_reply([{"i": 1, "translation": "", "emotion": "confused"}])
        cues = [{"i": 1, "text": "I hate this!"}, {"i": 2, "text": "Fine."}]
        with mock.patch.object(core, "_post", return_value=reply):
            items, _ = core.dub(cues, "es", "browser")
        self.assertEqual(items[0]["translation"], "I hate this!")
        self.assertEqual(items[0]["emotion"], "angry")      # invalid -> guessed
        self.assertEqual(items[1]["emotion"], "neutral")    # missing -> guessed

    def test_ai_failure_falls_back(self):
        with mock.patch.object(core, "_post", side_effect=OSError("offline")):
            items, warnings = core.dub([{"i": 0, "text": "Boom"}], "fr", "browser")
        self.assertEqual(items[0]["translation"], "Boom")
        self.assertTrue(warnings)
        self.assertIsNone(core.cache_get(core.make_key("an", "fr", "Boom")))


class Premium(unittest.TestCase):
    def test_audio_is_returned_and_saved(self):
        os.environ["TONEBRIDGE_MOCK"] = "1"
        os.environ["ELEVENLABS_API_KEY"] = "k"
        try:
            with mock.patch.object(core, "elevenlabs_speech", return_value=b"MP3DATA") as el:
                items, warnings = core.dub([{"i": 3, "text": "Stop it!"}], "same", "elevenlabs")
                self.assertEqual(warnings, [])
                self.assertEqual(items[0]["mime"], "audio/mpeg")
                core.dub([{"i": 3, "text": "Stop it!"}], "same", "elevenlabs")
                self.assertEqual(el.call_count, 1)  # second time from the database
            el_text = core.EL_TAGS["angry"] + "Stop it!"
            self.assertEqual(el_text, "[angry] Stop it!")
        finally:
            os.environ.pop("TONEBRIDGE_MOCK", None)
            os.environ.pop("ELEVENLABS_API_KEY", None)


def scribe_words():
    """What ElevenLabs speech-to-text returns: words, spacing, and sound events."""
    def w(text, a, b): return {"text": text, "start": a, "end": b, "type": "word"}
    sp = {"text": " ", "type": "spacing"}
    return [
        w("Where", 1.0, 1.3), sp, w("were", 1.3, 1.5), sp, w("you", 1.5, 1.7), sp, w("all", 1.7, 1.9), sp, w("this", 1.9, 2.1), sp, w("time?", 2.1, 2.6),
        {"text": "(laughs)", "start": 2.7, "end": 3.0, "type": "audio_event"}, sp,
        w("I'm", 4.5, 4.7), sp, w("so", 4.7, 4.8), sp, w("sorry", 4.8, 5.3),   # after a pause -> new line
        sp, w("really", 5.4, 5.8), sp, w("sorry.", 5.8, 6.4),
    ]


class Transcription(unittest.TestCase):
    def test_words_become_caption_lines(self):
        segs = core.group_words(scribe_words())
        self.assertEqual([s["text"] for s in segs], ["Where were you all this time?", "I'm so sorry really sorry."])
        self.assertEqual((segs[0]["start"], segs[0]["end"]), (1.0, 2.6))
        self.assertEqual((segs[1]["start"], segs[1]["end"]), (4.5, 6.4))

    def test_long_speech_is_split(self):
        words = []
        t = 0.0
        for i in range(60):
            words.append({"text": "word%d" % i, "start": t, "end": t + 0.3, "type": "word"})
            words.append({"text": " ", "type": "spacing"})
            t += 0.3
        segs = core.group_words(words)
        self.assertGreater(len(segs), 1)
        self.assertTrue(all(len(s["text"]) <= 110 for s in segs))

    def test_multipart_body(self):
        seen = {}

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b"{}"

        def fake_urlopen(req, timeout=0):
            seen["body"], seen["ctype"] = req.data, req.headers.get("Content-type")
            return FakeResp()

        with mock.patch.object(core.urllib.request, "urlopen", fake_urlopen):
            core._post_multipart("https://x", {}, {"model_id": "scribe_v1"}, "file", "my talk (1).mp3", b"AUDIO", "audio/mpeg")
        boundary = seen["ctype"].split("boundary=")[1]
        self.assertIn(("--" + boundary).encode(), seen["body"])
        self.assertIn(b'name="model_id"\r\n\r\nscribe_v1', seen["body"])
        self.assertIn(b'filename="my_talk__1_.mp3"', seen["body"])
        self.assertIn(b"AUDIO", seen["body"])

    def test_elevenlabs_path_and_cache(self):
        os.environ["ELEVENLABS_API_KEY"] = "k"
        reply = json.dumps({"language_code": "en", "words": scribe_words()}).encode()
        try:
            with mock.patch.object(core, "_post_multipart", return_value=reply) as post:
                segs, lang, _ = core.transcribe(b"audio-bytes-1", "a.mp3", "audio/mpeg")
                self.assertEqual(lang, "en")
                self.assertEqual(len(segs), 2)
                core.transcribe(b"audio-bytes-1", "a.mp3", "audio/mpeg")   # same file again
                self.assertEqual(post.call_count, 1)                       # served from the database
        finally:
            os.environ.pop("ELEVENLABS_API_KEY", None)

    def test_helpful_errors(self):
        with self.assertRaises(core.TranscribeError):
            core.transcribe(b"", "a.mp3")
        with self.assertRaises(core.TranscribeError) as big:
            core.transcribe(b"x" * (core.MAX_AUDIO_BYTES + 1), "a.mp3")
        self.assertIn("too big", str(big.exception))
        os.environ.pop("ELEVENLABS_API_KEY", None)
        with self.assertRaises(core.TranscribeError) as none:   # no key and faster-whisper not installed
            core.transcribe(b"some other audio", "a.mp3")
        self.assertIn("ELEVENLABS_API_KEY", str(none.exception))

    def test_elevenlabs_http_error_is_explained(self):
        os.environ["ELEVENLABS_API_KEY"] = "bad"
        err = core.urllib.error.HTTPError("u", 401, "no", {}, None)
        try:
            with mock.patch.object(core, "_post_multipart", side_effect=err):
                with self.assertRaises(core.TranscribeError) as ctx:
                    core.transcribe(b"audio-bytes-2", "a.mp3")
            self.assertIn("401", str(ctx.exception))
        finally:
            os.environ.pop("ELEVENLABS_API_KEY", None)


class WhoMayCallTheServer(unittest.TestCase):
    def check(self, headers, scheme="http"):
        import server
        from types import SimpleNamespace
        req = SimpleNamespace(headers=headers, url=SimpleNamespace(scheme=scheme))
        return server._allowed(req)

    def test_rules(self):
        self.assertTrue(self.check({}))                                                      # command line
        self.assertTrue(self.check({"origin": "chrome-extension://abc", "host": "127.0.0.1:8000"}))
        self.assertTrue(self.check({"origin": "http://127.0.0.1:8000", "host": "127.0.0.1:8000"}))   # our own Studio page
        self.assertFalse(self.check({"origin": "https://evil.example", "host": "127.0.0.1:8000"}))
        self.assertFalse(self.check({"origin": "http://evil.example:8000", "host": "evil.example:8000"}))  # DNS trick


if __name__ == "__main__":
    unittest.main()
