# free-tts — User Guide

## Overview

free-tts has two interfaces:

- **CLI** (`tts.py`): command-line synthesis. Fast, scriptable, good for batch work.
- **Web UI** (`server.py`): a single-page browser app. Good for interactive use, voice cloning, and browser playback.

Both produce MP3 files. The CLI writes to the path you specify; the web UI writes to `out/` and serves them for playback/download.

## CLI usage

### Basic synthesis

```bash
python tts.py "Hello world." -o out/hello.mp3
```

### Options

```
python tts.py [text] [options]

Text:
  text                       Text to synthesize (positional or via stdin)
  -f, --file FILE            Read text from a file

Output:
  -o, --output FILE          Output MP3 file (default: out.mp3)
  -e, --engine ENGINE        Engine: kokoro, coqui-clone, espeak-ng, pyttsx3, auto
  -v, --voice VOICE          Kokoro voice name (e.g. af_sarah, am_adam)
  -l, --lang LANGUAGE        Language code (en, en-us, fr, de, es, it, pt, ja, zh)
  -r, --ref FILE             Reference WAV for voice cloning (coqui-clone only)
  -c, --clean                Clean the reference audio (denoise + normalize + trim)
  --clean-voice VOICE        Use voice-specific loudness target when cleaning

Info:
  --list-engines             List available engines and their status
  --list-voices              List Kokoro voices
  --check                    Check engine import status (shorthand for --list-engines)
```

### Engine selection

The default engine is `auto`, which picks the best available engine:

1. If `--ref` is provided and coqui-clone is available → coqui-clone.
2. Otherwise → kokoro.
3. If kokoro isn't available → espeak-ng.
4. If espeak-ng isn't available → pyttsx3.

Override with `-e`:

```bash
python tts.py "Hello." -o hello.mp3 -e espeak-ng
python tts.py "Hello." -o hello.mp3 -e pyttsx3
```

### Voice cloning (CLI)

```bash
# clone with a reference file
python tts.py "Hello in my voice." -o clone.mp3 -e coqui-clone -r ref.wav -l en

# clone with cleaned reference
python tts.py "Hello in my voice." -o clone.mp3 -e coqui-clone -r ref.wav -l en --clean
```

The `-r` / `--ref` option takes a WAV file. The CLI normalizes it to 24kHz mono 16-bit WAV and passes it to XTTS as `speaker_wav`.

The `--clean` flag runs the cleaning pipeline on the reference before cloning: highpass filter (remove rumble), light FFT denoise, EBU R128 loudness normalization (I=-16 LUFS, TP=-1.5dB), and silence trimming. This improves cloning quality.

`--clean-voice` lets you specify a voice name so the cleaning pipeline can use voice-specific loudness targets (currently a placeholder; the cleaning chain uses fixed targets regardless).

### Batch synthesis

```bash
# synthesize a list of texts
for line in $(cat lines.txt); do
  python tts.py "$line" -o "out/$(echo $line | md5sum | cut -d' ' -f1).mp3"
done

# or use a file with one text per line
while read line; do
  python tts.py "$line" -o "out/$((i++)).mp3"
done < lines.txt
```

### Stdin

```bash
echo "Hello world." | python tts.py -o hello.mp3
cat story.txt | python tts.py -o story.mp3
```

## Web UI usage

### Starting the server

```bash
python server.py
# opens on http://127.0.0.1:5000
```

Override the port:

```bash
FREE_TTS_PORT=5050 python server.py
```

### The interface

The UI is a single page with these sections:

1. **Text input** — textarea for the text to synthesize.
2. **Engine / Voice / Language** — three dropdowns side by side.
   - Engine: auto / kokoro / coqui-clone / espeak-ng.
   - Voice: Kokoro voice list (populated from `/api/voices`). Only applies to kokoro.
   - Language: en, en-us, fr, de, es, it, pt, ja, zh.
3. **Reference audio panel** — for voice cloning.
   - Text field showing the current reference filename (disabled, shows "none — record or upload below" when empty).
   - **Upload** button — opens a file picker. Accepts .wav, .mp3, .mp4, .webm, .ogg, .flac.
   - **Record** button — starts the browser's microphone recording.
   - **Stop** button — stops the recording (appears during recording).
   - **Clean audio** checkbox — when ticked, the reference is denoised, loudness-normalized, and silence-trimmed before cloning. Takes a few seconds.
   - Hint text explaining the recording.
4. **Generate** button — synthesizes the text with the selected engine/voice.
5. **Audio bar** — appears after generation. Contains:
   - An `<audio>` player.
   - **Download MP3** link — downloads the generated file.
   - **Play** / **Stop** buttons.
   - **Clear** button — clears the player and status.
6. **Recent files** — a list of recent MP3/WAV files with Play / Download / Delete.
7. **Log** — a scrolling log of events (recording status, upload results, generation results, errors).

### Workflow: text → MP3

1. Type or paste text into the textarea.
2. Pick engine:
   - `auto` (default): uses kokoro for plain synthesis, coqui-clone if a reference is provided.
   - `kokoro`: neural synthesis with the selected voice.
   - `coqui-clone`: voice cloning — requires a reference.
   - `espeak-ng`: fast robotic synthesis.
3. Pick a voice (kokoro only) and language.
4. Optionally provide a reference for cloning (see below).
5. Click **Generate**.
6. The MP3 plays in the browser. Download it with the Download link, or Play/Stop from the audio bar.
7. The file appears in the Recent files list.

### Workflow: voice cloning

Voice cloning uses Coqui XTTS v2 to synthesize text in the voice of a reference audio.

1. Provide a reference audio:
   - **Record**: click Record, speak clearly for a few seconds, click Stop. The recording is saved locally and uploaded to the server.
   - **Upload**: click Upload, select a WAV/MP3/other audio file.
2. (Optional) Tick **Clean audio** to improve cloning quality. This denoises, normalizes loudness, and trims silence from the reference. Takes a few seconds.
3. The reference filename appears in the text field.
4. Select engine `auto` (or `coqui-clone`) and a language matching the reference.
5. Type text in the voice you want to clone.
6. Click **Generate**.
7. The MP3 is synthesized in the reference's voice.

Notes:
- The reference is normalized to 24kHz mono 16-bit WAV on the server.
- XTTS works best when the reference and text are in the same language.
- A longer, clearer reference gives better cloning.
- The voice dropdown is ignored for coqui-clone (the reference IS the voice).

### Cleaning the reference audio

The "Clean audio" checkbox runs an ffmpeg chain on the reference before cloning:

1. **Highpass filter** (80 Hz) — removes low-frequency rumble.
2. **FFT denoise** (`afftdn`, noise floor -25 dB) — light noise reduction.
3. **EBU R128 loudness normalization** (I=-16 LUFS, true peak -1.5 dB, LRA=11) — consistent loudness.
4. **Silence trimming** — removes leading and trailing silence (detected backwards and forwards, threshold -30 dB, duration 0.4s).

This makes the reference cleaner and more consistent, which improves XTTS voice embedding quality.

The cleaned file is named `ref_<timestamp>_norm.wav` (same as uncleaned, since cleaning is applied during normalization). The original raw upload is deleted after normalization.

The cleaning pipeline runs server-side on record/upload when `clean=true` is sent. It's also available in the CLI via `tts.py --clean`.

### File management

The Recent files panel shows MP3 and WAV files in `out/`, most recent first (up to 60).

- **Play** — plays the file in the browser audio player.
- **Download** — downloads the file.
- **Delete** — removes the file from `out/`.

You can also manage files directly in the `out/` directory.

### Keyboard shortcuts

There are no keyboard shortcuts by default. The UI is designed for mouse/touch interaction.

## Language support

The supported languages (via the dropdown):

| Code | Language |
|---|---|
| en | English |
| en-us | English (US) |
| fr | French |
| de | German |
| es | Spanish |
| it | Italian |
| pt | Portuguese |
| ja | Japanese |
| zh | Chinese |

Kokoro handles most of these. Coqui XTTS handles many languages (multilingual model). espeak-ng and pyttsx3 have more limited language support.

## Troubleshooting

### "Generate error" or "Error" in the UI

1. Open browser dev tools (F12) and check the Console for JS errors.
2. Check the server terminal for Python errors.
3. Common causes:
   - No text entered.
   - coqui-clone without a reference.
   - Engine not available (falls back, may fail).
   - ffmpeg not on PATH (MP3 export fails).
   - Reference file not found (deleted or wrong path).

### Recording doesn't work

- The browser must support the MediaRecorder API and getUserMedia.
- Allow microphone access when prompted.
- Some browsers require a secure context (HTTPS) for getUserMedia — localhost usually works as an exception.
- The recording is saved locally (as a Blob) and uploaded to the server. If the upload fails, check the server is running and the network is available.

### Upload doesn't work

- The file must be a supported audio format (.wav, .mp3, .mp4, .webm, .ogg, .flac).
- The server must be running and reachable.
- Large files may take time to upload — the progress indicator shows "Uploading…".

### "voices.map is not a function" or JS errors

This was a bug in an earlier version. If you still see it:
1. Hard-refresh the browser (Ctrl+F5 / Ctrl+Shift+R) to clear the cached `static/index.html`. Browsers cache the UI aggressively.
2. Restart the server (`python server.py`).
3. If it persists, check the browser console for the specific error.

### Clone produces low-quality audio

- Use a better reference: clear, quiet, several seconds of speech.
- Tick "Clean audio" to improve the reference.
- Ensure reference and text are in the same language.
- XTTS v2 is the model used; it's good but not perfect.

### Server won't start

- Port already in use: kill the process or use a different port (`FREE_TTS_PORT=5051`).
- Missing dependencies: run `python tts.py --check` to see engine status.
- ffmpeg not on PATH: install it.

## Tips

- Use `auto` engine for the best experience — it picks kokoro for plain text and coqui-clone when you provide a reference.
- Clean the reference audio for better cloning.
- Use longer references (5-10 seconds of clear speech) for better voice cloning.
- For batch work, use the CLI (`tts.py`) instead of the UI.
- The output is always MP3 — play it in any media player or the browser.
