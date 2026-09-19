# free-tts Documentation

Local TTS that generates MP3 files. Fully offline, free, Apache 2.0.

## What's here

- `tts.py` — CLI tool. Engines: kokoro, coqui-clone, espeak-ng, pyttsx3.
- `server.py` — Flask web UI on port 5000 (or `FREE_TTS_PORT`). Text→MP3, voice cloning via Coqui XTTS v2, reference recording/upload, browser playback.
- `out/` — output files (MP3 + reference WAVs).
- `docs/` — this documentation set.

## Documentation

| Document | Purpose |
|---|---|
| [01_getting_started](01_getting_started.md) | Install, verify, first synthesis, environment variables |
| [02_user_guide](02_user_guide.md) | Day-to-day: text→MP3, recording, uploading, voice cloning, file management, troubleshooting |
| [03_api_reference](03_api_reference.md) | Every endpoint, request/response schemas, error codes |
| [04_architecture](04_architecture.md) | Highly technical: process model, Coqui XTTS integration, Kokoro pipeline, ffmpeg pipeline, data flow, edge cases, the `_coqui_error` bug and how it was fixed, the cleaning pipeline |

## Quick verify

```bash
python tts.py --check            # engine import status
python tts.py "Hello world." -o out/test.mp3   # synthesize
python server.py                 # start UI on :5000
```

## Engines (auto-pick order)

1. **kokoro** — Kokoro TTS, neural, very natural (Apache 2.0, no TOS). Default.
2. **coqui-clone** — Coqui XTTS v2 zero-shot voice cloning. Requires a reference audio.
3. **espeak-ng** — fast, robotic, works offline without PyTorch.
4. **pyttsx3** — system TTS, lowest quality, broadest compatibility.

Override with `-e espeak-ng` or `-e pyttsx3` in CLI, or pick from the dropdown in the UI.

## Voices

Kokoro ships several voices. Run `python tts.py --list-voices` to see the full set on your install. The UI dropdown is populated from the server's `/api/voices` endpoint.

## Notes

- First run downloads the Kokoro model weights (one-time, ~tens of MB).
- Large texts are processed in one pass. For very long texts, split into chunks and run separately.
- Output is 192 kbps MP3. Change the bitrate in `tts.py` `export(format="mp3", bitrate="192k")`.
- Reference audio for cloning is normalized to 24kHz mono 16-bit WAV. Optionally clean it first (denoise + normalize + trim silence) via the "Clean audio" checkbox in the UI — this improves cloning quality.
- The web UI loads Kokoro in-process (pykokoro 0.9.x). The model is loaded once and reused across requests; first synthesis pays the load cost, subsequent ones are fast.
