# free-tts — API Reference

The web UI (`server.py`) exposes a REST API on the server port (default 5000, or `FREE_TTS_PORT`).

All endpoints return JSON. The base URL is `http://127.0.0.1:<port>`.

## Endpoints

### GET /health

Check that the server is running.

**Request:** none

**Response:**
```json
{"ok": true}
```

**Status:** 200

---

### GET /api/engines

List available engines and their import status. Runs `tts.py --list-engines` as a subprocess.

**Request:** none

**Response:**
```json
{"text": "...", "rc": 0}
```

- `text` — the output of `tts.py --list-engines`.
- `rc` — the return code (0 = success).

**Status:** 200

---

### GET /api/voices

List available Kokoro voices.

**Request:** none

**Response:**
```json
{"voices": ["af_sarah", "af_nicole", "am_adam", ...]}
```

- `voices` — array of voice name strings.

**Status:** 200

---

### POST /api/generate

Synthesize text to MP3.

**Request body (JSON):**
```json
{
  "text": "Hello world.",          // required: text to synthesize
  "engine": "auto",                // optional: kokoro | coqui-clone | espeak-ng | pyttsx3 | auto
  "voice": "af_sarah",             // optional: Kokoro voice name (default: af_sarah)
  "lang": "en",                    // optional: language code (default: en)
  "ref": "/api/files/ref_xxx_norm.wav"  // optional: reference audio path (for coqui-clone)
}
```

**Response (success):**
```json
{
  "ok": true,
  "file": {
    "name": "tts_1234567890.mp3",
    "path": "D:\\Coding\\free-tts\\out\\tts_1234567890.mp3",
    "size": 12345
  }
}
```

- `ok` — `true` on success.
- `file.name` — the output filename.
- `file.path` — the full filesystem path.
- `file.size` — the file size in bytes.

**Response (error):**
```json
{
  "error": "error message here",
  "stdout": "...",
  "stderr": "...",
  "rc": 1
}
```

or

```json
{
  "error": "error message here"
}
```

**Status:** 200 on success, 400 on bad request, 500 on generation failure.

**Errors:**
- `400 No text.` — `text` is empty or missing.
- `400 coqui-clone requires a reference audio (record or upload).` — coqui-clone selected but no `ref`.
- `400 reference file not found: ...` — the `ref` path doesn't exist.
- `500 coqui-clone not available: ...` — Coqui XTTS not importable.
- `500 clone failed: ...` — XTTS synthesis failed.
- `500 Generation failed.` — CLI synthesis failed (non-zero exit or output missing).

**Notes:**
- The `ref` field is a URL path like `/api/files/ref_xxx_norm.wav`. The server resolves it to a filesystem path under `out/`.
- For coqui-clone, the `voice` field is ignored (the reference is the voice).
- For kokoro, the `voice` field selects the Kokoro voice.
- The `lang` field selects the language for both kokoro and coqui-clone.

---

### POST /api/record

Record a reference audio from the browser's microphone.

**Request body (JSON):**
```json
{
  "audio_b64": "data:audio/wav;base64,<base64-data>",  // required: base64-encoded audio
  "clean": true                                          // optional: run cleaning pipeline
}
```

- `audio_b64` — base64-encoded audio (the browser's MediaRecorder output, as a data URL).
- `clean` — if `true`, runs the cleaning pipeline (denoise + normalize + trim silence) on the reference.

**Response (success):**
```json
{
  "path": "ref_1234567890_norm.wav",
  "size": 12345
}
```

- `path` — the output filename (basename only, no directory).
- `size` — the file size in bytes.

**Response (error):**
```json
{
  "error": "No audio data."
}
```

**Status:** 200 on success, 400 on bad request.

**Notes:**
- The audio is decoded from base64, written to `out/ref_<ts>.wav`, then normalized to 24kHz mono 16-bit WAV as `out/ref_<ts>_norm.wav`.
- If `clean=true`, the cleaning pipeline is applied during normalization.
- The raw `ref_<ts>.wav` is deleted after normalization.
- The `path` returned is the basename only (e.g., `ref_1234567890_norm.wav`), not a full path.

---

### POST /api/upload

Upload a reference audio file.

**Request:** multipart/form-data with:
- `file` — the audio file (required).
- `clean` — if `"1"`, runs the cleaning pipeline (optional).

**Response (success):**
```json
{
  "path": "ref_1234567890_norm.wav",
  "size": 12345
}
```

**Response (error):**
```json
{
  "error": "No file."
}
```

**Status:** 200 on success, 400 on bad request.

**Notes:**
|- Same normalization and cleaning behavior as `/api/record`.
|- The `path` returned is the basename only.

---

### POST /api/align

Generate SRT captions for an existing audio file in `out/`, with optional speaker diarization. No TTS generation required — select an existing MP3/WAV and get word-level aligned captions.

**Request body (JSON):**
```json
{
  "file": "tts_1234567890.mp3",   // required: filename in out/
  "text": "Optional text...",      // optional: paste text for better word alignment
  "lang": "en",                    // optional: language code (default: en)
  "diarize": false                 // optional: enable speaker diarization (default: false)
}
```

- `file` — filename of an audio file in `out/` (e.g. an MP3 generated earlier).
- `text` — if provided, words are matched greedily to the Whisper transcription for accurate per-segment timing. If omitted, Whisper transcription segments are used directly.
- `lang` — language code for Whisper transcription.
- `diarize` — if `true`, runs pyannote.audio speaker diarization. Each SRT segment is prefixed with `[Speaker N]`. Falls back to alignment-only if diarization fails.

**Response (success):**
```json
{
  "ok": true,
  "captions": {
    "name": "srt_1234567890.srt",
    "path": "...",
    "size": 533,
    "fallback": false
  }
}
```

- `captions.name` — the generated SRT filename.
- `captions.size` — the SRT file size in bytes.
- `captions.fallback` — present and `true` if Whisper alignment failed and text-weighted division was used.

**Status:** 200 on success, 400 on bad request.

**Notes:**
- Uses faster-whisper for word-level forced alignment. Model `Systran/faster-whisper-base` is tried first, falling back to `tiny`.
- Speaker diarization uses `pyannote/speaker-diarization-3.1` via pyannote.audio.

---

### GET /api/files

List files in the `out/` directory.

**Request:** none

**Response:**
```json
{
  "files": [
    {"name": "tts_1234567890.mp3", "size": 12345, "type": "mp3"},
    {"name": "ref_1234567890_norm.wav", "size": 6789, "type": "wav"},
    {"name": "srt_1234567890.srt", "size": 533, "type": "srt"}
  ]
}
```

- `files` — array of file objects.
- Each file has `name` (filename), `size` (bytes), `type` (extension).

**Status:** 200

---

### GET /api/files/<name>

Download a file from `out/`.

**Request:** none (the file is identified by the URL path).

**Response:** the file bytes with appropriate Content-Type:
|- `audio/mpeg` for .mp3
|- `audio/wav` for .wav
|- `application/x-subrip` for .srt
|- `application/octet-stream` for other files

**Status:** 200 on success, 400 on bad name, 404 if not found.

**Notes:**
- The `name` is sanitized: only simple filenames are allowed (no `/`, `\`, `..`).
- The file is served with `download_name` set to the filename.

---

### DELETE /api/files/<name>

Delete a file from `out/`.

**Request:** none (the file is identified by the URL path).

**Response:**
```json
{"ok": true}
```

**Status:** 200 on success, 400 on bad name, 404 if not found.

**Notes:**
- The `name` is sanitized: only simple filenames are allowed.
- Returns 404 if the file doesn't exist.

---

## Error codes summary

| Status | Meaning |
|---|---|
| 200 | Success |
| 400 | Bad request (missing text, no file, bad name, etc.) |
| 404 | File not found |
| 500 | Generation failure, engine not available, clone failed |

All error responses include an `error` field with a message.

---

## Authentication

None. The API is unauthenticated and intended for local use only.

---

## CORS

None. The API is same-origin only (served from the same Flask app as the UI).
