# Tonebridge MVP

Dubbing that keeps the speaker's feeling. This is a working first version (MVP):
a **Chrome extension** plus a small **Python server** on your own computer.

**What it does, in simple words**
1. The extension reads the video's captions (subtitles).
2. It sends each line to your server.
3. The server translates the line and decides the feeling: angry, happy, sad, excited or neutral.
4. The extension speaks the line in that feeling, at the right moment of the video, and lowers the original sound.
5. You see a small label on the video, for example "Tonebridge 😠 Angry", and the dubbed text.

The server saves every finished line in a small file (`backend/cache.db`), so the same line is never paid for twice.

---

## Set it up (about 10 minutes, Windows)

### Step 1. Install Python
Download Python 3.10 or newer from python.org. In the installer, **tick "Add Python to PATH"**.

### Step 2. Start the server
Double-click **`start_server.bat`**. The first time it installs two small packages. Then you will see:

```
Tonebridge server running.
  Demo page: http://127.0.0.1:8000/demo/
```
Leave this window open while you use Tonebridge. (To stop it, close the window.)

### Step 3. Add the extension to Chrome
1. Open Chrome and go to `chrome://extensions`.
2. Turn on **Developer mode** (top right).
3. Click **Load unpacked** and choose the **`extension`** folder.
4. Click the puzzle-piece icon in Chrome and **pin** Tonebridge.

### Step 4. Try it on the demo page (no keys needed)
1. Open **http://127.0.0.1:8000/demo/**
2. Click the Tonebridge icon, switch it **on**, set **Dub language** to **"Same language (emotion only)"**.
3. Press play on the video. You will hear each line with its own feeling, and see the emotion label.

Without any key, the feeling is a simple guess from the words and punctuation. For the real thing, add a key (next step).

---

## Turn on the AI (translation + real emotion detection)

1. Get an API key at **console.anthropic.com** and add a few dollars of credit.
2. In the `backend` folder, copy `.env.example` to a new file called **`.env`**.
3. Paste your key after `ANTHROPIC_API_KEY=`.
4. Close the server window and double-click `start_server.bat` again.

Now you can choose **Hindi, English, Spanish, French, German or Japanese** as the dub language.
The model name is in `.env.example` (`LLM_MODEL`), so you can change it any time.

## Premium voice (optional)

1. Get a key at **elevenlabs.io** and paste it after `ELEVENLABS_API_KEY=` in `.env`.
2. In the extension, set **Voice** to **Premium (ElevenLabs)**.

If the key is missing or ElevenLabs fails, Tonebridge quietly uses the free browser voice and shows a note in the popup.

## Try it on YouTube
Open a YouTube video that has captions, switch Tonebridge on, press play.
If YouTube does not give us the caption file, Tonebridge switches to **Live mode**: turn captions on with the **CC** button and it will dub each line as it appears (a little delay is normal).

## Make captions from an audio or video file (Studio)

Have a file with **no captions**? Use the Studio page.

1. With the server running, open **http://127.0.0.1:8000/studio/**
2. Choose (or drag in) your audio or video file. Maximum **25 MB**.
3. Click **Make captions**. Tonebridge listens and writes the captions with times.
4. Fix any mistakes in the text boxes. Click **Update the player**.
5. Click the Tonebridge icon, switch it **on**, choose a language, and press play on the video in step 3. It is dubbed with feeling.
6. Want the captions for something else? Click **Download captions (.vtt)**.

**The listening needs one of these (pick one):**
- **ElevenLabs key** (easiest): put `ELEVENLABS_API_KEY=` in your `.env` file. The same key also gives the premium voice.
- **Free, on your own computer:** run `pip install faster-whisper` once. The first time you use it, it downloads a speech model (about 150 MB). It is slower, but costs nothing and the file never leaves your computer.

The same file is never listened to twice. The captions are saved in the database.

---

## What is in the folder

| Folder / file | What it is |
|---|---|
| `extension/` | The Chrome extension (what the viewer sees) |
| `backend/core.py` | The brain: listening (speech to captions), emotion, translation, premium voice, saving to the database |
| `backend/server.py` | The small server the extension talks to |
| `demo/` | A short test video with emotional captions |
| `studio/` | The page where you add an audio or video file and get captions |
| `tests/` | Automatic tests |
| `start_server.bat` | Double-click to start the server |

## Honest limits of this MVP (good to know before the demo)

- **Needs captions.** It works on videos that have captions: normal web video players and YouTube. For a file with no captions, use the Studio page. It will not work on sites with protected or custom players (for example Netflix or Prime Video).
- **Audio files only get words and times.** The Studio listens for the words. It does not yet measure the speaker's loudness or pitch, so the feeling is still decided from the words.
- **The "Listening" step is not in yet.** The slides say we measure loudness, speed and pitch of the original voice (librosa). This MVP decides the feeling from the **words** only. That is the next thing to add.
- **Voice is not cloned.** The free voice is your browser's own voice, with speed, pitch and loudness changed to match the feeling. It is much better than flat, but it is not a human actor. ElevenLabs sounds far more natural.
- **Orpheus (free open-source voice) is not included.** It needs a graphics card (GPU), so it is a Round 2 job. The code is built so the voice step can be swapped.
- **The server uses Starlette, not FastAPI.** FastAPI is built on top of Starlette, so it is the same idea. Moving to FastAPI later is a small change.
- **Your captions are sent to Anthropic (and ElevenLabs if you use it)** to translate and speak them. If you use the ElevenLabs listening, your audio file is sent to ElevenLabs too. Keep your keys private. Never put `.env` on GitHub.
- The server only listens on your own computer, and only accepts requests from the extension.

## Run the automatic tests (optional)

```
python -m unittest discover -s tests -v
```
(`tests/e2e_test.py` loads the extension into a test browser. It needs `pip install playwright` and `playwright install chromium`.)
