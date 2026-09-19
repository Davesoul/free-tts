# free-tts — Getting Started

## What it is

free-tts is a local, offline, free (Apache 2.0) text-to-speech tool that generates MP3 files. It has two interfaces:

- **CLI** (`tts.py`): command-line synthesis with multiple engines.
- **Web UI** (`server.py`): a single-page browser app with text input, engine/voice selection, voice cloning via reference audio, file management, and browser playback.

## Prerequisites

- **Python 3.8+** (Python 3.14 used here at `C:\Python314\python.exe`).
- **pip** and the packages listed in `requirements.txt`.
- **ffmpeg** on PATH — required for MP3 export. On Windows, `imageio-ffmpeg` provides one automatically; if export fails, install system ffmpeg.
- **espeak-ng** (optional, system binary) — recommended for the espeak-ng engine. Without it, the espeak-ng engine falls back to pyttsx3.

## Install

```bash
# option 1: one-shot install script (Windows, uses C:\Python314\python.exe)
install.bat

# option 2: manual
pip install -r requirements.txt

# espeak-ng system package (optional but recommended):
#   Windows : choco install espeak-ng  or  download from https://github.com/espeak-ng/espeak-ng/releases
#   Linux   : sudo apt install espeak-ng
#   macOS   : brew install espeak
```

`requirements.txt`:

```
pykokoro>=0.9.0
soundfile>=0.12.0
TTS>=0.27.0          # coqui-tts (XTTS v2 voice cloning)
pyttsx3>=2.90
espeakng-loader>=0.2.0
# system: ffmpeg on PATH (MP3 export); espeak-ng optional for espeak-ng engine
```

Note: `pydub` appears in `install.bat` but is NOT used by `tts.py` — `tts.py` calls ffmpeg directly. It's harmless to leave installed.

## First run

### CLI

```bash
# synthesize some text
python tts.py "Hello world, this is a quick test of free-tts." -o out/test_cli.mp3

# read text from a file
python tts.py -f story.txt -o story.mp3

# use a specific Kokoro voice
python tts.py "Bonjour." -o hello.mp3 -v af_nicole

# see what engines are available
python tts.py --list-engines

# list Kokoro voices
python tts.py --list-voices
```

### Web UI

```bash
# start the server (default port 5000)
python server.py

# or a different port
FREE_TTS_PORT=5050 python server.py
```

Then open `http://127.0.0.1:5000` (or your port) in a browser.

The UI has:
- A text area for input.
- Engine dropdown (auto / kokoro / coqui-clone / espeak-ng).
- Voice dropdown (populated from `/api/voices`).
- Language dropdown (en, en-us, fr, de, es, it, pt, ja, zh).
- A reference audio panel: Record (mic) or Upload (file) to provide a voice-cloning reference.
- A "Clean audio" checkbox — when ticked, the reference is denoised, loudness-normalized, and silence-trimmed before cloning.
- Generate button → MP3 plays in the browser, downloadable.
- A file list with Play / Download / Delete for recent outputs.

## Environment variables

| Variable | Default | Effect |
|---|---|---|
| `FREE_TTS_PY` | `C:/Python314/python.exe` | Python executable used by `tts.py` (CLI). Set to your python path if different. |
| `FREE_TTS_PORT` | `5000` | Port for the Flask web UI (`server.py`). |
| `FREE_TTS_HOST` | `127.0.0.1` | Host the Flask server binds to. |

Example:

```bash
FREE_TTS_PY=/usr/bin/python3 FREE_TTS_PORT=8080 python server.py
```

## Verify it works

```bash
# engine import status
python tts.py --check
# should print status for kokoro, coqui-clone, espeak-ng, pyttsx3

# synthesize
python tts.py "Hello world." -o out/verify.mp3
# should produce out/verify.mp3

# web UI health
curl http://127.0.0.1:5000/health
# should return {"ok": true}
```

## First synthesis (web UI)

1. Type text in the textarea.
2. Pick engine: `auto` (recommended — uses kokoro unless you provide a reference, then clones).
3. Pick a voice from the dropdown (only applies to kokoro).
4. Pick a language.
5. Optionally: Record or Upload a reference audio for voice cloning. Tick "Clean audio" to improve cloning quality.
6. Click **Generate**.
7. The MP3 plays in the browser. Use Download to save it, or Play/Stop from the audio bar.
8. Recent files appear below — Play, Download, or Delete them.

## Engines in detail

### kokoro (default)

Neural TTS via pykokoro. Very natural, Apache 2.0, no TOS. First run downloads weights. Supports multiple English voices and some non-English languages.

```bash
python tts.py "Hello." -o hello.mp3 -v af_sarah -l en
```

### coqui-clone (voice cloning)

Coqui XTTS v2 zero-shot voice cloning. Requires a reference WAV (your voice or any voice). The UI records or uploads a reference, normalizes it to 24kHz mono 16-bit WAV, and passes it to XTTS as `speaker_wav`.

```bash
# CLI: clone with a reference file
python tts.py "Hello in my voice." -o clone.mp3 -e coqui-clone -r ref.wav -l en
```

The clone engine ignores the voice dropdown (the reference IS the voice). The language parameter matters — XTTS works best when the reference and text are in the same language.

First use loads the XTTS v2 model (can be slow, ~hundreds of MB). Subsequent uses reuse the loaded model.

### espeak-ng

Fast, robotic, works offline without PyTorch. Good fallback when other engines aren't available.

```bash
python tts.py "Hello." -o hello.mp3 -e espeak-ng
```

Requires the `espeak-ng` system binary on PATH. If absent, falls back to pyttsx3.

### pyttsx3

System TTS, lowest quality, broadest compatibility. Used as a last-resort fallback.

```bash
python tts.py "Hello." -o hello.mp3 -e pyttsx3
```

## Voices

Kokoro ships several voices. Run `python tts.py --list-voices` to see the full set. The server's `/api/voices` endpoint exposes them to the UI dropdown.

Common English voices:
- `af_sarah` — default, female
- `af_nicole` — female
- `am_adam` — male
- `af_alloy`, `af_heart`, `af_jessica`, `af_kore`, `af_libritts5338`, `af_nova`, `af_river`, `af_sky`
- `am_echo`, `am_eric`, `am_fenrir`, `am_liam`, `am_michael`, `am_onyx`, `am_puck`, `am_santa`, `am_vincentprice`
- plus British (bf_*, bm_*), and various other language voices

The full list is in `server.py`'s `known` list (the `/api/voices` response).

## Output format

- MP3, 192 kbps (CLI) or ~162 kbps (web UI, via ffmpeg).
- 24kHz sample rate.
- Mono.

Change the bitrate in `tts.py`: `export(format="mp3", bitrate="192k")`.

## Troubleshooting

### "Engine not available" / fallback to pyttsx3

- kokoro: missing `pykokoro` or `soundfile`, or model download failed.
- coqui-clone: missing `TTS` (Coqui) package, or model download failed.
- espeak-ng: `espeak-ng` binary not on PATH; falls back to pyttsx3.
- pyttsx3: always available if installed.

Run `python tts.py --check` to see per-engine status.

### MP3 export fails

ffmpeg is not on PATH. Install it:
- Windows: `choco install ffmpeg` or use `imageio-ffmpeg`.
- Linux: `sudo apt install ffmpeg`.
- macOS: `brew install ffmpeg`.

### Web UI shows "Generate error" or "Error"

- Check the browser console (F12) for JS errors.
- Check the server terminal for Python errors.
- For coqui-clone: ensure a reference audio is provided (record or upload).
- For kokoro: ensure a voice is selected.

### Server won't start (port already in use)

Kill the process on the port:

```bash
# find PID
netstat -ano | grep LISTENING | grep 5000
# kill it (Windows)
taskkill /F /PID <pid>
```

Or use a different port:

```bash
FREE_TTS_PORT=5050 python server.py
```

### Reference audio doesn't clone well

- Use a clear, quiet recording with minimal background noise.
- Tick "Clean audio" in the UI — this denoises, normalizes loudness, and trims silence.
- Use a longer reference (several seconds of speech).
- Ensure the reference and text are in the same language.

### "voices.map is not a function" or similar JS errors

This was a bug in an earlier version. The current code is fixed. If you still see it, hard-refresh the browser (Ctrl+F5 / Ctrl+Shift+R) to clear the cached `static/index.html`. Browsers cache the UI aggressively.

## Next steps

- Read [02_user_guide](02_user_guide.md) for day-to-day usage.
- Read [03_api_reference](03_api_reference.md) for the HTTP API.
- Read [04_architecture](04_architecture.md) for a deep technical walkthrough.
