# free-tts — Architecture & Technical Reference

## Overview

free-tts is a local TTS tool with two components:

- **CLI** (`tts.py`): a standalone command-line tool that synthesizes text to MP3 using one of several engines.
- **Web UI** (`server.py`): a Flask app that serves a single-page browser UI and proxies synthesis requests to the CLI or to Coqui XTTS directly (for voice cloning).

The two share engine logic: `server.py` calls `tts.py` as a subprocess for kokoro/espeak/pyttsx3, and calls Coqui XTTS directly in-process for coqui-clone (to avoid reloading the model per request).

## Repository layout

```
free-tts/
├── tts.py              # CLI tool
├── server.py           # Flask web UI
├── static/
│   └── index.html      # generated UI (from server.py's _build_static())
├── out/                # output directory (MP3 + reference WAVs)
├── docs/               # documentation
│   ├── 01_getting_started.md
│   ├── 02_user_guide.md
│   ├── 03_api_reference.md
│   └── 04_architecture.md
├── requirements.txt
├── install.bat
└── README.md
```

## Engine selection & process model

### Auto-pick order

1. If `--ref` is provided and coqui-clone is available → coqui-clone.
2. Otherwise → kokoro (in-process, model reused across requests).
3. If kokoro isn't available → espeak-ng (subprocess).
4. If espeak-ng isn't available → pyttsx3 (subprocess).

### Process model

- **kokoro**: loaded in-process via `_get_kokoro()`. The `KokoroPipeline` object is created once on the first synthesis request and reused for all subsequent kokoro/auto requests. This eliminates the subprocess-spawn + import + model-load overhead (~1–3s per request) that the original subprocess-based design incurred. The model is loaded once (~1–2s on first call) and reused indefinitely.

- **coqui-clone**: loaded in-process via `_get_coqui()` (Coqui XTTS v2). Same reuse pattern as kokoro.

- **espeak-ng**: runs as a subprocess calling the system `espeak-ng` binary. No in-process caching (it's a thin system-call wrapper).

- **pyttsx3**: runs as a subprocess calling `tts.py` (which uses pyttsx3's Python API internally). No in-process caching.

The `_run_cli()` helper (which spawns `python tts.py ...` as a subprocess) is retained for espeak-ng and pyttsx3, and for the `/api/engines` endpoint (which needs to run `tts.py --list-engines`).

### Entry point

```python
if __name__ == "__main__":
    main()
```

`main()` parses CLI arguments with `argparse` and dispatches to the appropriate engine.

### Engine selection (auto-pick)

The `auto` engine picks the best available engine in order:

1. If `--ref` is provided and coqui-clone is available → coqui-clone.
2. Otherwise → kokoro.
3. If kokoro isn't available → espeak-ng.
4. If espeak-ng isn't available → pyttsx3.

The selection is implemented as a chain of try/except import checks at module load time:

```python
try:
    from pykokoro import Pipeline as _KokoroPipeline
    KOKORO_READY = True
except Exception as e:
    KOKORO_READY = False
    _KokoroPipeline = None
```

Each engine has a `_READY` flag and a `_ERROR` message captured at import time.

### Kokoro engine

Uses pykokoro's `Pipeline` to synthesize text.

```python
def kokoro_synth(text, voice, lang, out_wav):
    pipeline = _KokoroPipeline()
    wav = pipeline(text, voice=voice, lang=lang)
    soundfile.write(out_wav, wav, samplerate=24000)
```

Then `soundfile` writes a 24kHz mono WAV, and ffmpeg converts it to MP3:

```python
def export_mp3(wav_path, mp3_path, bitrate="192k"):
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(wav_path),
         "-codec:a", "libmp3lame", "-b:a", bitrate, str(mp3_path)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
```

### Coqui XTTS engine (clone)

Uses Coqui TTS's `TTS` API directly. The model is loaded once and reused.

```python
def _get_coqui():
    global _coqui, _coqui_ready
    if not _coqui_ready:
        _coqui = _CoquiTTS("tts_models/multilingual/multi-dataset/xtts_v2", progress_bar=False)
        _coqui_ready = True
    return _coqui
```

Synthesis:

```python
tts = _get_coqui()
tts.tts_to_file(text=text, speaker_wav=str(ref_path), language=lang, file_path=str(wav_path))
```

The reference WAV is normalized to 24kHz mono 16-bit before being passed to XTTS. After synthesis, the WAV is converted to MP3 via ffmpeg.

### espeak-ng engine

Calls the `espeak-ng` system binary:

```python
def espeak_synth(text, out_wav):
    subprocess.run(
        ["espeak-ng", "-w", str(out_wav), text],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
```

If `espeak-ng` is not on PATH, the import fails and the engine falls back to pyttsx3.

### pyttsx3 engine

Uses pyttsx3's Python API:

```python
def pyttsx3_synth(text, out_wav):
    import pyttsx3
    engine = pyttsx3.init()
    engine.save_to_file(text, str(out_wav))
    engine.runAndWait()
```

### Cleaning pipeline (CLI)

The `--clean` flag runs an ffmpeg chain on the reference before cloning:

```bash
ffmpeg -y -i input.wav \
  -af "highpass=f=80,afftdn=nf=-25,loudnorm=I=-16:TP=-1.5:LRA=11,\
       areverse,silenceremove=stop_periods=-1:stop_duration=0.4:stop_threshold=-30dB,\
       areverse,\
       silenceremove=start_periods=1:start_duration=0.4:start_threshold=-30dB" \
  -ar 24000 -ac 1 -sample_fmt s16 output_norm.wav
```

Steps:
1. **Highpass** (80 Hz) — remove low-frequency rumble.
2. **afftdn** (noise floor -25 dB) — light FFT-based denoising.
3. **loudnorm** (EBU R128: I=-16 LUFS, TP=-1.5 dB, LRA=11) — loudness normalization.
4. **Silence trimming** — removes leading and trailing silence:
   - `areverse` + `silenceremove` (stop) — trim trailing silence.
   - `areverse` — flip back.
   - `silenceremove` (start) — trim leading silence.

Output is 24kHz mono 16-bit WAV, ready for XTTS cloning.

The `--clean-voice` option is accepted but currently uses fixed loudness targets regardless of voice (a placeholder for voice-specific targets).

### Output naming

The output MP3 is named based on the text (sanitized) and a timestamp:

```python
safe = "".join(c if c.isalnum() or c in "._- " else "_" for c in text)[:60]
ts = int(time.time() * 1000)
out_name = f"tts_{ts}.mp3"
```

When `--clean` is used, the reference output is named `ref_<ts>_norm.wav` (cleaned during normalization).

## `server.py` — Flask web UI

### Setup

```python
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 80 * 1024 * 1024  # 80 MB max upload
```

Environment variables:
- `FREE_TTS_PY` — Python executable for subprocess calls (default `C:/Python314/python.exe`).
- `FREE_TTS_PORT` — server port (default 5000).
- `FREE_TTS_HOST` — server host (default 127.0.0.1).

### Subprocess runner

The CLI engines (kokoro, espeak-ng, pyttsx3) are run as subprocesses:

```python
def _run_cli(args, stdin_data=None):
    proc = subprocess.run(
        [PY, str(CLI)] + args,
        input=stdin_data, capture_output=True, text=True,
        timeout=600, cwd=str(HERE),
    )
    return proc.returncode, proc.stdout, proc.stderr
```

This allows the server to reuse the CLI's engine logic without importing it directly.

### Coqui XTTS in-process

For voice cloning, the server imports Coqui TTS directly and loads the model once:

```python
try:
    from TTS.api import TTS as _CoquiTTS
    _coqui = None
    _coqui_ready = False
    _coqui_imported = True
    _coqui_error = None
except Exception as e:
    _coqui = None
    _coqui_ready = False
    _coqui_imported = False
    _coqui_error = str(e)
    _CoquiTTS = None
```

The `_coqui_error = None` initialization in the success path is important: previously it was only set in the `except` block, so accessing `(_coqui_error or "?")` when Coqui imported fine raised `NameError`, which Flask turned into an HTML 500 response instead of JSON. This caused the browser to see `SyntaxError: Unexpected token '<'` when it tried to parse the HTML as JSON. The fix: always set `_coqui_error` in both paths.

The guard `if not _coqui_imported:` (instead of `if not _coqui_ready and _coqui is None`) correctly checks whether Coqui was importable at all.

### Endpoints

See [03_api_reference](03_api_reference.md) for the full API.

Key implementation details:

#### /api/generate

1. Parses the JSON body.
2. If engine is `coqui-clone`:
   - Checks `_coqui_imported`.
   - Resolves the `ref` URL path to a filesystem path.
   - Checks the reference file exists.
   - Calls `_get_coqui()` to get the XTTS model.
   - Runs `tts.tts_to_file()` with the reference as `speaker_wav`.
   - Converts the output WAV to MP3 via ffmpeg.
   - Returns the MP3 info.
3. Otherwise (kokoro / espeak-ng / pyttsx3):
   - For kokoro: loads the pipeline in-process via `_get_kokoro()`, calls `pipeline(text, voice=voice, lang=lang)` which returns an `AudioResult` (numpy audio + sample rate), writes WAV via `soundfile`, converts to MP3 via ffmpeg.
   - For espeak-ng / pyttsx3: builds CLI args and calls `_run_cli()`.
   - Returns the MP3 info or an error.

#### /api/record

1. Decodes the base64 audio from the request.
2. Writes it to `out/ref_<ts>.wav`.
3. Normalizes to 24kHz mono 16-bit WAV as `out/ref_<ts>_norm.wav`, optionally with the cleaning pipeline.
4. Deletes the raw `ref_<ts>.wav`.
5. Returns the basename of the normalized file.

#### /api/upload

1. Saves the uploaded file to `out/ref_<ts>.wav`.
2. Normalizes to 24kHz mono 16-bit WAV as `out/ref_<ts>_norm.wav`, optionally with the cleaning pipeline.
3. Deletes the raw `ref_<ts>.wav`.
4. Returns the basename of the normalized file.

#### /api/files and /api/files/<name>

List and serve/delete files in `out/`. Names are sanitized to prevent path traversal.

### The UI (static/index.html)

The UI is a single-page app generated by `_build_static()` and written to `static/index.html` by `_ensure_static()` at startup.

Key UI behaviors:

- **Voice dropdown**: populated from `/api/voices` on page load.
- **Recording**: uses the browser's MediaRecorder API to capture microphone audio. The recording is saved as a Blob, converted to a data URL, base64-encoded, and sent to `/api/record`.
- **Upload**: uses a file input and FormData to send the file to `/api/upload`.
- **Generation**: sends the text, engine, voice, lang, and optional ref to `/api/generate`. On success, sets the audio player source and download link.
- **File list**: fetched from `/api/files` and displayed with Play/Download/Delete buttons.
- **Log**: a scrolling log of events.

#### The `api()` helper

The UI uses an `api()` helper for fetch calls:

```javascript
async function api(path, opts) {
    const r = fetch(path, opts);
    const j = r.then(res => res.text());
    const p = j.then(t => {
        try { return JSON.parse(t) }
        catch (e) { return { error: 'response not JSON', body: t } }
    });
    return p.then(x => {
        if (!x.error) return x;
        L('Server error: ' + (x.body || '').slice(0, 600));
        return x;
    });
}
```

This helper handles non-JSON responses gracefully: if the server returns HTML (e.g., a Flask 500 error page), the helper logs the body and returns an error object instead of throwing. This prevents `SyntaxError: Unexpected token '<'` in the browser.

The `clean` checkbox state is sent as `clean: $('#cleanRef').checked` (a boolean) in the record request body, and as `fd.append('clean', '1')` in the upload FormData.

---

## Data flow

### CLI synthesis (kokoro)

```
text → tts.py → pykokoro.Pipeline → wav (24kHz mono) → ffmpeg → mp3 (192k)
                                                                ↑
                                                          soundfile.write
```

### CLI voice cloning (coqui-clone)

```
text + ref.wav → tts.py → normalize ref to 24kHz mono → XTTS.tts_to_file → wav → ffmpeg → mp3
```

### Web UI synthesis (kokoro)

**Before (subprocess per request):**
```
browser: text + engine + voice + lang → POST /api/generate → server.py → _run_cli([...]) → tts.py → mp3 → response
```

Each kokoro request spawned a new Python process, imported pykokoro, and loaded the ONNX model — adding ~1–3s of overhead per request.

**After (in-process, model reused):**
```
browser: text + engine + voice + lang → POST /api/generate → server.py → _get_kokoro() → KokoroPipeline(text, voice=, lang=) → AudioResult → soundfile.write(wav) → ffmpeg → mp3 → response
```

The Kokoro pipeline is now loaded in-process (via `_get_kokoro()`, same pattern as `_get_coqui()` for XTTS). The first request pays the model-load cost (~1–2s); subsequent requests reuse the loaded pipeline and skip the subprocess spawn + import + model reload entirely.

The `_run_cli()` subprocess runner is retained for espeak-ng and pyttsx3, which are system binaries / thin wrappers that don't benefit from in-process caching.

### Web UI voice cloning (coqui-clone)

```
browser: record/upload ref → POST /api/record or /api/upload → server.py → normalize ref (+ clean) → ref_<ts>_norm.wav → browser: text + engine=coqui-clone + ref → POST /api/generate → server.py → _get_coqui() → XTTS.tts_to_file(ref_<ts>_norm.wav) → wav → ffmpeg → mp3 → response → browser: play + download
```

### Reference audio lifecycle

1. Browser records or uploads raw audio.
2. Server receives it, writes `out/ref_<ts>.wav`.
3. Server normalizes to `out/ref_<ts>_norm.wav` (24kHz mono 16-bit), optionally cleaning.
4. Server deletes `out/ref_<ts>.wav`.
5. Server returns `ref_<ts>_norm.wav` (basename) to the browser.
6. Browser stores the filename and sends it as `ref` in `/api/generate`.
7. Server resolves `ref` to `out/ref_<ts>_norm.wav` and uses it for cloning.
8. After cloning, the reference file remains in `out/` (can be deleted via the UI or manually).

---

## Edge cases & known issues

### `_coqui_error` NameError (fixed)

**Problem:** When Coqui XTTS imported successfully, `_coqui_error` was only set in the `except` block. Accessing `(_coqui_error or "?")` in the generate endpoint raised `NameError`, which Flask caught and returned as an HTML 500 page. The browser saw `SyntaxError: Unexpected token '<'` when trying to parse the HTML as JSON.

**Fix:** Set `_coqui_error = None` in the success path (try block) so it's always defined.

### `_coqui_imported` guard (fixed)

**Problem:** The clone endpoint used `if not _coqui_ready and _coqui is None` as the guard. This was fragile because `_coqui` could be `None` even when imported (it's initialized to `None` and set later).

**Fix:** Use `if not _coqui_imported:` which directly checks whether the import succeeded.

### JS `vs.map` undefined (fixed)

**Problem:** The voice dropdown population used `vs.map(...)` but `vs` was never defined — it should have been `voices.map(...)`.

**Fix:** Changed to `voices.map(...)`.

### JS `else` without braces (fixed)

**Problem:** `else voice.innerHTML='...'` — the `else` body was a single statement, but the following `}` was interpreted as closing the `try` block, leaving `catch` orphaned.

**Fix:** Wrapped the else body in braces: `else { voice.innerHTML='...'; }`.

### JS `reader.readAsDataURL` missing (fixed)

**Problem:** The `FileReader.onload` handler was set up but `reader.readAsDataURL(recordedBlob)` was never called, so the upload never fired and the recording appeared to hang.

**Fix:** Added `reader.readAsDataURL(recordedBlob)` after setting up the onload handler.

### JS `onstop` callback not closed (fixed)

**Problem:** The `mediaRecorder.onstop=()=>{...}` callback body was missing the closing `};`, causing a syntax error.

**Fix:** Added the closing `};`.

### `recordBtn` element missing (fixed)

**Problem:** The JS referenced `recordBtn.addEventListener(...)` but no `<button id="recordBtn">` existed in the HTML.

**Fix:** Added the Record button to the HTML template.

### Reference path Windows backslashes (fixed)

**Problem:** The server returned `str(norm_path)` (full Windows path with backslashes). The browser's `j.path.split('/').pop()` failed because backslashes don't split on `/`.

**Fix:** Return `norm_path.name` (basename only) from the server, and use `(j.path.split('\\').pop() || j.path).split('/').pop() || j.path` in the browser for robustness.

### `credentials: 'include'` on upload (fixed)

**Problem:** The upload request included `credentials: 'include'`, which is unnecessary for same-origin requests and can cause issues.

**Fix:** Removed.

### jQuery `is(':checked')` (fixed)

**Problem:** The clean checkbox used `$('#cleanRef').is(':checked')` which is jQuery syntax, but jQuery is not loaded.

**Fix:** Changed to `$('#cleanRef').checked` (native DOM property).

### Browser caching of static/index.html

**Problem:** Browsers cache `static/index.html` aggressively. After fixing bugs in the UI, users may still see the old version.

**Fix:** Hard-refresh (Ctrl+F5 / Ctrl+Shift+R) or use incognito. The server regenerates `static/index.html` from `_build_static()` on every startup, so a restart picks up changes.

### ffmpeg not on PATH

**Problem:** MP3 export requires ffmpeg. If it's not on PATH, synthesis fails.

**Fix:** Install ffmpeg: `choco install ffmpeg` (Windows), `sudo apt install ffmpeg` (Linux), `brew install ffmpeg` (macOS). On Windows, `imageio-ffmpeg` provides one automatically.

### espeak-ng binary not on PATH

**Problem:** The espeak-ng engine requires the `espeak-ng` system binary. Without it, the engine falls back to pyttsx3.

**Fix:** Install espeak-ng: `choco install espeak-ng` (Windows), `sudo apt install espeak-ng` (Linux), `brew install espeak` (macOS).

### pykokoro 0.9.x API compatibility

**Problem:** pykokoro 0.9.x changed `KokoroPipeline.__init__` to require a `PipelineConfig` argument (calling `KokoroPipeline()` without args raises `missing 1 required positional argument: 'config'`). Also, `__call__`/`run` no longer accepts an `out=` parameter — it returns an `AudioResult` (numpy `.audio` + `.sample_rate`) instead of writing a WAV file directly. `server.py` was written for the older API and failed with the `__init__` error when the web UI tried kokoro synthesis.

**Fix:** In `server.py` `_get_kokoro()`, construct the pipeline as `KokoroPipeline(PipelineConfig())`. In the `/api/generate` endpoint, replace `kokoro(text, voice=, lang=, out=str(wav_path))` with:
```
result = kokoro(text, voice=voice, lang=lang)
soundfile.write(str(wav_path), result.audio, result.sample_rate)
```
The WAV is then converted to MP3 via the existing `_kokoro_to_mp3()` ffmpeg step as before.

### Coqui XTTS model download

**Problem:** First use of coqui-clone downloads the XTTS v2 model (~hundreds of MB). This can be slow and may fail on poor connections.

**Fix:** Ensure a stable internet connection for first use. The model is cached and reused on subsequent calls.

### Large files

**Problem:** The server limits uploads to 80 MB (`MAX_CONTENT_LENGTH`). Very large reference files may be rejected.

**Fix:** Use smaller reference files (a few seconds of speech is sufficient for cloning).

---

## Design decisions

### Why Coqui XTTS is called in-process (not via subprocess)

Calling XTTS via subprocess would require spawning a new Python process and loading the model each time, which is slow and memory-inefficient. By importing Coqui directly in `server.py`, the model is loaded once and reused across requests.

The trade-off: if Coqui crashes, it can take down the server. The try/except in `_get_coqui()` and the clone endpoint mitigates this.

### Why the CLI is called as a subprocess for other engines

The kokoro, espeak-ng, and pyttsx3 engines are run as subprocesses via `_run_cli()`. This isolates engine crashes from the server and allows the CLI to be used independently.

### Why the cleaning pipeline uses ffmpeg

ffmpeg is already required by the project (for MP3 export). Using ffmpeg filters for cleaning avoids adding new Python dependencies. The chain (highpass + afftdn + loudnorm + silenceremove) is a standard ffmpeg audio processing pipeline.

### Why the reference is normalized to 24kHz mono 16-bit

Coqui XTTS expects 24kHz mono audio. Normalizing on upload/record ensures the reference is in the correct format regardless of the source (mic, uploaded file, etc.).

### Why the `clean` checkbox is opt-in

Cleaning adds latency (a few seconds of ffmpeg processing). Making it opt-in lets users choose between speed (no cleaning) and quality (cleaning).

---

## Modules and their responsibilities

| Module | Responsibility |
|---|---|
| `tts.py` | CLI argument parsing, engine dispatch, synthesis, MP3 export, cleaning pipeline |
| `server.py` | Flask app, API endpoints, Coqui XTTS in-process, subprocess runner for CLI engines, UI generation |
| `static/index.html` | Single-page browser UI (generated from `server.py`'s `_build_static()`) |
| `soundfile` | WAV read/write (used by kokoro engine) |
| `pykokoro` | Kokoro TTS neural synthesis |
| `TTS` (Coqui) | XTTS v2 voice cloning |
| `pyttsx3` | System TTS fallback |
| `espeak-ng` | Fast robotic TTS (system binary) |
| `ffmpeg` | MP3 export, reference normalization, cleaning pipeline |
