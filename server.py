#!/usr/bin/env python3
"""
free-tts-ui — simple local web UI for the free-tts CLI.
Serve:  python server.py
Open:   http://127.0.0.1:5000

CLI still works independently:  python tts.py "text" -o out.mp3
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    from flask import Flask, Response, jsonify, request, send_file
except Exception as e:
    print(f"ERROR: flask not available: {e}", file=sys.stderr)
    sys.exit(1)

PY = os.environ.get("FREE_TTS_PY", "C:/Python314/python.exe")
HERE = Path(__file__).resolve().parent
CLI = HERE / "tts.py"
OUT_DIR = HERE / "out"
OUT_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 80 * 1024 * 1024


# ---------------------------------------------------------------------------
# Kokoro direct (in-process, for the UI's kokoro engine)
# ---------------------------------------------------------------------------
try:
    from pykokoro import KokoroPipeline as _KokoroPipeline

    _kokoro = None
    _kokoro_ready = False
    _kokoro_imported = True
    _kokoro_error = None
except Exception as e:
    _kokoro = None
    _kokoro_ready = False
    _kokoro_imported = False
    _kokoro_error = str(e)
    _KokoroPipeline = None


def _get_kokoro():
    global _kokoro, _kokoro_ready
    if not _kokoro_ready:
        from pykokoro import PipelineConfig as _PC
        _kokoro = _KokoroPipeline(_PC())
        _kokoro_ready = True
    return _kokoro


def _kokoro_to_mp3(wav_path, mp3_path, bitrate="192k"):
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(wav_path),
         "-codec:a", "libmp3lame", "-b:a", bitrate, str(mp3_path)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    wav_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Coqui XTTS direct (used by clone engine in the UI)
# ---------------------------------------------------------------------------
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


def _get_coqui():
    global _coqui, _coqui_ready
    if not _coqui_ready:
        _coqui = _CoquiTTS("tts_models/multilingual/multi-dataset/xtts_v2", progress_bar=False)
        _coqui_ready = True
    return _coqui


# ---------------------------------------------------------------------------
# SRT caption generation
# ---------------------------------------------------------------------------

def _make_srt(srt_path, text, duration_sec):
    """Write an SRT file from the input text.
    Splits text by line breaks; segments weighted by character count
    for more realistic timing than equal-duration division.
    """
    segments = [s.strip() for s in text.split("\n") if s.strip()]
    if not segments:
        segments = [text.strip()]
    n = len(segments)
    weights = [len(s) for s in segments]
    total_w = sum(weights)
    with open(srt_path, "w", encoding="utf-8") as f:
        cum = 0.0
        for i, seg in enumerate(segments, 1):
            if total_w > 0:
                seg_dur = duration_sec * (len(seg) / total_w)
            else:
                seg_dur = duration_sec / n
            start = cum
            end = cum + seg_dur
            cum = end
            f.write(f"{i}\n")
            f.write(f"{_fmt_time(start)} --> {_fmt_time(end)}\n")
            f.write(f"{seg}\n\n")


def _fmt_time(sec):
    """Format seconds as SRT timestamp: HH:MM:SS,mmm"""
    sec = max(sec, 0)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int((sec - int(sec)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ---------------------------------------------------------------------------
# Forced alignment SRT (word-level timestamps via faster-whisper)
# ---------------------------------------------------------------------------
_whisper_model = None
_whisper_ready = False
_WHISPER_MODELS = ["Systran/faster-whisper-base", "Systran/faster-whisper-tiny"]


def _get_whisper():
    """Lazy-load and cache the best available faster-whisper model for alignment.
    Tries 'base' first for better word-level accuracy; falls back to 'tiny'."""
    global _whisper_model, _whisper_ready
    if not _whisper_ready:
        try:
            from faster_whisper import WhisperModel
            for _name in _WHISPER_MODELS:
                try:
                    _whisper_model = WhisperModel(
                        _name, device="cpu", compute_type="int8",
                    )
                    print(f"WHISPER-DBG loaded: {_name}", file=sys.stderr)
                    break
                except Exception as _e:
                    print(f"WHISPER-DBG model '{_name}' failed: {_e}", file=sys.stderr)
                    _whisper_model = None
        except Exception as e:
            print(f"WHISPER-DBG import failed: {e}", file=sys.stderr)
            _whisper_model = None
        _whisper_ready = True
    return _whisper_model


# ---------------------------------------------------------------------------
# Speaker diarization (pyannote.audio)
# ---------------------------------------------------------------------------
_diarizer = None
_diarizer_ready = False


def _get_diarizer():
    """Lazy-load and cache the pyannote.audio speaker diarization pipeline.

    Requires HF_TOKEN env var for gated models (visit
    hf.co/pyannote/speaker-diarization-3.1 to accept terms).
    Returns None if pyannote.audio is not available or model fails to load.
    """
    global _diarizer, _diarizer_ready
    if not _diarizer_ready:
        token = os.environ.get("HF_TOKEN")
        try:
            from pyannote.audio import Pipeline
            _diarizer = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1",
                token=token,
            )
            print("DIARIZER-DBG loaded: pyannote/speaker-diarization-3.1", file=sys.stderr)
        except Exception as e:
            print(f"DIARIZER-DBG load failed: {e}", file=sys.stderr)
            _diarizer = None
        _diarizer_ready = True
    return _diarizer


def _make_srt_aligned(srt_path, text, audio_path, lang="en"):
    """Generate SRT with word-level timestamps via Whisper forced alignment.

    Transcribes the audio with faster-whisper ``word_timestamps=True``,
    then aligns word timestamps to the original text segments (split by
    newlines).  Returns True on success, None on failure so the caller
    can fall back to _make_srt.
    """
    model = _get_whisper()
    if model is None:
        return None

    import re

    def _norm(s):
        """Lowercase, strip punctuation for word comparison."""
        return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()

    try:
        wl = lang if lang and lang != "auto" else "en"
        if wl in ("en-us",):
            wl = "en"

        segments, _info = model.transcribe(
            str(audio_path),
            word_timestamps=True,
            language=wl,
        )
        segments = list(segments)  # materialize generator for multiple iterations

        # Collect whisper words: (normalized_word, start, end)
        wwords = []
        for seg in segments:
            for w in seg.words:
                nw = _norm(w.word)
                if nw:
                    wwords.append((nw, w.start, w.end))
        if not wwords:
            return None

        # Split original text into segments (by newlines)
        text_segs = [s.strip() for s in text.split("\n") if s.strip()]
        if not text_segs:
            if text.strip():
                text_segs = [text.strip()]
            else:
                text_segs = None

        if text_segs is None:
            # No text provided — use Whisper segmentation directly
            with open(srt_path, "w", encoding="utf-8") as f:
                for i, seg in enumerate(segments, 1):
                    f.write(f"{i}\n")
                    f.write(f"{_fmt_time(seg.start)} --> {_fmt_time(seg.end)}\n")
                    f.write(f"{seg.text.strip()}\n\n")
            return True

        # Normalized word list per text segment
        seg_word_lists = [
            [w for w in (_norm(x) for x in seg.split()) if w]
            for seg in text_segs
        ]

        # Greedy monotonic matching: walk through whisper words in order
        wi = 0
        seg_times = []
        for seg_wlist in seg_word_lists:
            if not seg_wlist:
                seg_times.append(None)
                continue
            seg_start = seg_end = None
            for target in seg_wlist:
                for j in range(wi, len(wwords)):
                    if wwords[j][0] == target:
                        if seg_start is None:
                            seg_start = wwords[j][1]
                        seg_end = wwords[j][2]
                        wi = j + 1
                        break
            seg_times.append((seg_start, seg_end) if (seg_start is not None) else None)

        # Fall back if any segment couldn't be aligned
        if any(t is None for t in seg_times):
            print(
                f"WHISPER-DBG: {sum(1 for t in seg_times if t is None)}"
                f"/{len(text_segs)} segments unaligned, falling back",
                file=sys.stderr,
            )
            return None

        # Ensure monotonic, non-overlapping timestamps
        result = []
        for i, t in enumerate(seg_times):
            if result and t[0] < result[-1][1]:
                t = (result[-1][1], max(t[1], result[-1][1] + 0.05))
            result.append(t)

        with open(srt_path, "w", encoding="utf-8") as f:
            for i, seg in enumerate(text_segs):
                start, end = result[i]
                f.write(f"{i + 1}\n")
                f.write(f"{_fmt_time(start)} --> {_fmt_time(end)}\n")
                f.write(f"{seg}\n\n")

        return True
    except Exception as e:
        print(f"WHISPER-DBG alignment error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return None


def _make_srt_diarized(srt_path, text, audio_path, lang="en"):
    """Generate SRT with word-level timestamps AND speaker diarization.

    Uses faster-whisper for word alignment and pyannote.audio for speaker
    diarization. Each SRT segment is prefixed with ``[Speaker N]``.

    If *text* is non-empty, words are matched greedily to the original
    text segments (same algorithm as ``_make_srt_aligned``). If *text* is
    empty, Whisper transcription segments are used directly as the SRT
    content.
    """
    model = _get_whisper()
    if model is None:
        return None

    import re

    def _norm(s):
        return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()

    try:
        wl = lang if lang and lang != "auto" else "en"
        if wl in ("en-us",):
            wl = "en"

        segments, _info = model.transcribe(
            str(audio_path),
            word_timestamps=True,
            language=wl,
        )
        segments = list(segments)  # materialize generator for multiple iterations

        # --- speaker diarization ---
        diarizer = _get_diarizer()
        speaker_segments = []
        if diarizer is not None:
            try:
                diarization = diarizer(str(audio_path))
                for turn, _, speaker in diarization.itertracks(yield_label=True):
                    speaker_segments.append((turn.start, turn.end, speaker))
            except Exception as e:
                print(f"DIARIZER-DBG run failed: {e}", file=sys.stderr)

        if not speaker_segments:
            print("DIARIZER-DBG no speaker segments, falling back to alignment-only",
                  file=sys.stderr)
            return None

        # --- collect words with speaker labels ---
        def _word_speaker(w_start, w_end):
            for spk_start, spk_end, spk_label in speaker_segments:
                if w_start >= spk_start and w_end <= spk_end:
                    return spk_label
                if w_start < spk_end and w_end > spk_start:
                    return spk_label
            return "?"

        wwords = []
        for seg in segments:
            for w in seg.words:
                nw = _norm(w.word)
                if nw:
                    wwords.append((nw, w.word.strip(), w.start, w.end,
                                   _word_speaker(w.start, w.end)))

        if not wwords:
            return None

        # --- split text into segments ---
        text_segs = [s.strip() for s in text.split("\n") if s.strip()]
        if not text_segs:
            text_segs = [text.strip()] if text.strip() else None

        if text_segs:
            # Greedy monotonic word matching (same as _make_srt_aligned but
            # also tracks speaker labels)
            seg_word_lists = [
                [w for w in (_norm(x) for x in seg.split()) if w]
                for seg in text_segs
            ]

            wi = 0
            seg_times = []
            for seg_wlist in seg_word_lists:
                if not seg_wlist:
                    seg_times.append(None)
                    continue
                seg_start = seg_end = None
                seg_speakers = set()
                for target in seg_wlist:
                    for j in range(wi, len(wwords)):
                        if wwords[j][0] == target:
                            if seg_start is None:
                                seg_start = wwords[j][2]
                            seg_end = wwords[j][3]
                            seg_speakers.add(wwords[j][4])
                            wi = j + 1
                            break
                seg_times.append(
                    (seg_start, seg_end, seg_speakers)
                    if (seg_start is not None) else None
                )

            if any(t is None for t in seg_times):
                n_bad = sum(1 for t in seg_times if t is None)
                print(f"WHISPER-DBG: {n_bad}/{len(text_segs)} segments "
                      f"unaligned, falling back", file=sys.stderr)
                return None

            # Ensure monotonic, non-overlapping timestamps
            result = []
            for i, t in enumerate(seg_times):
                if result and t[0] < result[-1][1]:
                    t = (result[-1][1],
                         max(t[1], result[-1][1] + 0.05), t[2])
                result.append(t)

            with open(srt_path, "w", encoding="utf-8") as f:
                for i, seg in enumerate(text_segs):
                    start, end, speakers = result[i]
                    spk_str = ", ".join(sorted(speakers)) if speakers else "?"
                    f.write(f"{i + 1}\n")
                    f.write(f"{_fmt_time(start)} --> {_fmt_time(end)}\n")
                    f.write(f"[Speaker {spk_str}] {seg}\n\n")
        else:
            # No text provided — use Whisper transcription segments directly
            with open(srt_path, "w", encoding="utf-8") as f:
                for i, seg in enumerate(segments, 1):
                    spk = _word_speaker(seg.start, seg.end)
                    f.write(f"{i}\n")
                    f.write(f"{_fmt_time(seg.start)} --> {_fmt_time(seg.end)}\n")
                    f.write(f"[Speaker {spk}] {seg.text.strip()}\n\n")

        return True
    except Exception as e:
        print(f"WHISPER-DBG diarized alignment error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------
@app.route("/health")
def health():
    return jsonify({"ok": True})


@app.route("/api/engines")
def engines():
    rc, out, err = _run_cli(["--list-engines"])
    return jsonify({"text": out or err, "rc": rc})


@app.route("/api/voices")
def voices():
    # Known-good Kokoro voices (detected earlier; no per-voice synthesis here).
    known = [
        "af_alloy", "af_ameliaearhart", "af_aoede", "af_bella", "af_heart",
        "af_jessica", "af_kore", "af_libritts5338", "af_nicole", "af_nova",
        "af_river", "af_sarah", "af_sky", "am_adam", "am_echo", "am_eric",
        "am_fenrir", "am_liam", "am_libritts1272", "am_libritts6241",
        "am_michael", "am_onyx", "am_puck", "am_santa", "am_vincentprice",
        "bf_alice", "bf_emma", "bf_isabella", "bf_janegoodall", "bf_lily",
        "bm_daniel", "bm_davidattenborough", "bm_fable", "bm_george",
        "bm_lewis", "ef_dora", "em_alex", "em_santa", "ff_siwis",
        "hf_alpha", "hf_beta", "hm_omega", "hm_psi", "if_sara", "im_nicola",
        "jf_alpha", "jf_gongitsune", "jf_nezumi", "jf_tebukuro", "jm_kumo",
        "pf_dora", "pm_alex", "pm_santa", "zf_xiaobei", "zf_xiaoni",
        "zf_xiaoxiao", "zf_xiaoyi", "zm_yunjian", "zm_yunxi", "zm_yunxia",
        "zm_yunyang",
    ]
    return jsonify({"voices": known})


@app.route("/api/generate", methods=["POST"])
def generate():
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "No text."}), 400

    engine = data.get("engine") or "auto"
    voice = data.get("voice") or "af_sarah"
    lang = data.get("lang") or "en"
    ref = data.get("ref")  # path to reference wav already on disk

    print(f"GENERATE-DBG: engine={engine}, ref={ref}, _kokoro_imported={_kokoro_imported}", file=sys.stderr)

    safe = "".join(c if c.isalnum() or c in "._- " else "_" for c in text)[:60]
    ts = int(time.time() * 1000)
    out_name = f"tts_{ts}.mp3"
    out_path = OUT_DIR / out_name

    # Clone path: run XTTS directly so we can accept TOS once and not poll subprocess
    if engine == "coqui-clone":
        if not _coqui_imported:
            return jsonify({"error": "coqui-clone not available: " + (_coqui_error or "?")}), 500
        if not ref:
            return jsonify({"error": "coqui-clone requires a reference audio (record or upload)."}), 400
        # ref may be a URL path like /api/files/foo.wav — resolve to filesystem
        ref_path = None
        if ref.startswith("/api/files/"):
            ref_path = OUT_DIR / ref[len("/api/files/"):]
        else:
            ref_path = Path(ref)
        if not ref_path.exists():
            return jsonify({"error": "reference file not found: " + str(ref_path)}), 400
        try:
            tts = _get_coqui()
            wav_path = out_path.with_suffix(".wav")
            tts.tts_to_file(text=text, speaker_wav=str(ref_path), language=lang, file_path=str(wav_path))
            # Read exact WAV duration for accurate SRT timing (must read before deleting WAV)
            import soundfile as _sf
            wav_info = _sf.info(str(out_path.with_suffix(".wav")))
            duration = wav_info.duration
            # convert to mp3
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(out_path.with_suffix(".wav")),
                 "-codec:a", "libmp3lame", "-b:a", "192k", str(out_path)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            out_path.with_suffix(".wav").unlink(missing_ok=True)
            info = {"path": str(out_path), "size": out_path.stat().st_size, "name": out_name}
            srt_name = f"tts_{ts}.srt"
            srt_path = OUT_DIR / srt_name
            try:
                if _make_srt_aligned(srt_path, text, str(out_path), lang=lang):
                    print("CLONE-DBG SRT: word-level aligned", file=sys.stderr)
                else:
                    _make_srt(srt_path, text, duration)
                srt_info = {"path": str(srt_path), "size": srt_path.stat().st_size, "name": srt_name}
            except Exception:
                srt_info = None
            return jsonify({"ok": True, "file": info, "captions": srt_info})
        except Exception as e:
            return jsonify({"error": "clone failed: " + str(e)}), 500

    # In-process paths: kokoro and coqui-clone use direct calls (model already loaded).
    # espeak-ng and pyttsx3 still go through the CLI subprocess (they are system binaries / thin wrappers).

    if engine == "kokoro" or (engine == "auto" and ref is None):
        if not _kokoro_imported:
            return jsonify({"error": "kokoro not available: " + (_kokoro_error or "?")}), 500
        try:
            kokoro = _get_kokoro()
            wav_path = out_path.with_suffix(".wav")
            result = kokoro(text, voice=voice, lang=lang)
            import soundfile as sf
            sf.write(str(wav_path), result.audio, result.sample_rate)
            _kokoro_to_mp3(wav_path, out_path)
            info = {"path": str(out_path), "size": out_path.stat().st_size, "name": out_name}
            # Generate SRT captions — try Whisper alignment, fall back to text-weighted
            srt_name = f"tts_{ts}.srt"
            srt_path = OUT_DIR / srt_name
            duration_sec = len(result.audio) / result.sample_rate
            try:
                if _make_srt_aligned(srt_path, text, str(out_path), lang=lang):
                    print("KOKO-DBG SRT: word-level aligned", file=sys.stderr)
                else:
                    _make_srt(srt_path, text, duration_sec)
                    print("KOKO-DBG SRT: text-weighted fallback", file=sys.stderr)
                srt_info = {"path": str(srt_path), "size": srt_path.stat().st_size, "name": srt_name}
            except Exception as e:
                print(f"KOKO-DBG SRT FAIL: {e}", file=sys.stderr)
                import traceback; traceback.print_exc(file=sys.stderr)
                srt_info = None
            return jsonify({"ok": True, "file": info, "captions": srt_info})
        except Exception as e:
            return jsonify({"error": "kokoro failed: " + str(e)}), 500

    # CLI path for espeak-ng / pyttsx3 / auto with ref (falls through to clone)
    args = ["-o", str(out_path)]
    if engine != "auto":
        args += ["-e", engine]
    if engine == "kokoro" or engine == "auto":
        args += ["-v", voice]
    args += ["-l", lang]
    if text:
        args.append(text)

    stdin = "y\n" if engine == "coqui-clone" else None
    rc, out_txt, err_txt = _run_cli(args, stdin_data=stdin)

    if rc != 0 or not out_path.exists():
        return jsonify({
            "error": "Generation failed.",
            "stdout": out_txt,
            "stderr": err_txt,
            "rc": rc,
        }), 500

    info = {"path": str(out_path), "size": out_path.stat().st_size, "name": out_name}
    # Generate SRT captions — use actual MP3 duration from ffprobe
    srt_name = f"tts_{ts}.srt"
    srt_path = OUT_DIR / srt_name
    try:
        import subprocess as _sp
        probe = _sp.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(out_path)],
            capture_output=True, text=True, check=True,
        )
        duration = float(probe.stdout.strip())
        if _make_srt_aligned(srt_path, text, str(out_path), lang=lang):
            print("CLI-DBG SRT: word-level aligned", file=sys.stderr)
        else:
            _make_srt(srt_path, text, duration)
        srt_info = {"path": str(srt_path), "size": srt_path.stat().st_size, "name": srt_name}
    except Exception:
        # Fall back to MP3-size estimate
        _make_srt(srt_path, text, out_path.stat().st_size / 20000)
        srt_info = {"path": str(srt_path), "size": srt_path.stat().st_size, "name": srt_name}
    return jsonify({"ok": True, "file": info, "captions": srt_info})


@ app.route("/api/record", methods=["POST"])
def record():
    data = request.get_json(force=True, silent=True) or {}
    blob_b64 = data.get("audio_b64")
    clean = data.get("clean")  # optional bool: run denoising/normalization chain
    if not blob_b64:
        return jsonify({"error": "No audio data."}), 400
    raw = base64.b64decode(blob_b64.split(",", 1)[-1])
    ts = int(time.time() * 1000)
    ref_path = OUT_DIR / f"ref_{ts}.wav"
    ref_path.write_bytes(raw)

    # Normalize to 16-bit mono WAV 24kHz, optionally with cleaning chain
    try:
        norm_path = OUT_DIR / f"ref_{ts}_norm.wav"
        cmd = ["ffmpeg", "-y", "-i", str(ref_path)]
        if clean:
            # highpass remove rumble, EBU R128 loudness norm,
            # then trim leading/trailing silence (reversed twice to catch both ends)
            # afftdn (FFT denoise) is intentionally omitted — it can produce
            # unstable output that crashes XTTS speaker encoding with
            # "index out of range in self"
            cmd += [
                "-af", "highpass=f=80,loudnorm=I=-16:TP=-1.5:LRA=11,"
                       "areverse,silenceremove=stop_periods=-1:stop_duration=0.4:"
                       "stop_threshold=-30dB,areverse,"
                       "silenceremove=start_periods=1:start_duration=0.4:start_threshold=-30dB",
            ]
        cmd += ["-ar", "24000", "-ac", "1", "-sample_fmt", "s16", str(norm_path)]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Fall back to uncleaned if cleaned audio is too short for XTTS
        if norm_path.exists() and norm_path.stat().st_size < 4000:
            norm_path.unlink(missing_ok=True)
            raise RuntimeError("cleaned audio too short")
        ref_path.unlink(missing_ok=True)
        return jsonify({"path": norm_path.name, "size": norm_path.stat().st_size})
    except Exception:
        return jsonify({"path": ref_path.name, "size": ref_path.stat().st_size})


@app.route("/api/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file."}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "No file."}), 400
    clean = request.form.get("clean") or False  # optional bool from multipart form
    ts = int(time.time() * 1000)
    ref_path = OUT_DIR / f"ref_{ts}.wav"
    f.save(str(ref_path))

    # Normalize to 16-bit mono WAV 24kHz, optionally with cleaning chain
    try:
        norm_path = OUT_DIR / f"ref_{ts}_norm.wav"
        cmd = ["ffmpeg", "-y", "-i", str(ref_path)]
        if clean:
            # highpass remove rumble, EBU R128 loudness norm,
            # then trim leading/trailing silence (reversed twice to catch both ends)
            # afftdn (FFT denoise) is intentionally omitted — it can produce
            # unstable output that crashes XTTS speaker encoding with
            # "index out of range in self"
            cmd += [
                "-af", "highpass=f=80,loudnorm=I=-16:TP=-1.5:LRA=11,"
                       "areverse,silenceremove=stop_periods=-1:stop_duration=0.4:"
                       "stop_threshold=-30dB,areverse,"
                       "silenceremove=start_periods=1:start_duration=0.4:start_threshold=-30dB",
            ]
        cmd += ["-ar", "24000", "-ac", "1", "-sample_fmt", "s16", str(norm_path)]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Fall back to uncleaned if cleaned audio is too short for XTTS
        if norm_path.exists() and norm_path.stat().st_size < 4000:
            norm_path.unlink(missing_ok=True)
            raise RuntimeError("cleaned audio too short")
        return jsonify({"path": norm_path.name, "size": norm_path.stat().st_size})
    except Exception:
        return jsonify({"path": ref_path.name, "size": ref_path.stat().st_size})


@app.route("/api/align", methods=["POST"])
def align():
    """Generate SRT captions for an existing audio file with optional diarization.

    Request JSON:
      file     – filename in out/ (e.g. "tts_123.mp3")
      text     – optional text for alignment (if omitted, Whisper transcription is used)
      lang     – language code (default "en")
      diarize  – bool, enable speaker diarization via pyannote.audio

    Response JSON: {ok:true, captions:{path,size,name,fallback?}}
    """
    data = request.get_json(force=True, silent=True) or {}
    file_name = data.get("file")
    text = (data.get("text") or "").strip()
    lang = data.get("lang") or "en"
    diarize = data.get("diarize", False)

    if not file_name:
        return jsonify({"error": "No file specified."}), 400

    audio_path = OUT_DIR / file_name
    if not audio_path.exists():
        return jsonify({"error": "File not found: " + file_name}), 400

    ts = int(time.time() * 1000)
    srt_name = f"srt_{ts}.srt"
    srt_path = OUT_DIR / srt_name

    if diarize:
        result = _make_srt_diarized(srt_path, text, str(audio_path), lang=lang)
        if result:
            print("ALIGN-DBG SRT: diarized", file=sys.stderr)
        else:
            result = _make_srt_aligned(srt_path, text or "", str(audio_path), lang=lang)
            if result:
                print("ALIGN-DBG SRT: alignment-only (diarization unavailable)",
                      file=sys.stderr)
    else:
        result = _make_srt_aligned(srt_path, text or "", str(audio_path), lang=lang)
        if result:
            print("ALIGN-DBG SRT: word-level aligned", file=sys.stderr)

    if result and srt_path.exists():
        return jsonify({"ok": True, "captions": {
            "path": str(srt_path), "size": srt_path.stat().st_size, "name": srt_name}})
    else:
        # Fallback to text-weighted division
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
                capture_output=True, text=True, check=True,
            )
            duration = float(probe.stdout.strip())
        except Exception:
            duration = audio_path.stat().st_size / 20000
        _make_srt(srt_path, text or "", duration)
        return jsonify({"ok": True, "captions": {
            "path": str(srt_path), "size": srt_path.stat().st_size,
            "name": srt_name, "fallback": True}})


@app.route("/api/files")
def list_files():
    try:
        files = []
        for p in sorted(OUT_DIR.glob("*.mp3"), reverse=True):
            files.append({"name": p.name, "size": p.stat().st_size, "type": "mp3"})
        for p in sorted(OUT_DIR.glob("*.wav"), reverse=True):
            files.append({"name": p.name, "size": p.stat().st_size, "type": "wav"})
        for p in sorted(OUT_DIR.glob("*.srt"), reverse=True):
            files.append({"name": p.name, "size": p.stat().st_size, "type": "srt"})
        return jsonify({"files": files})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/files/<name>")
def files(name):
    # sanitize: only allow simple names
    if not name or "/" in name or "\\" in name or ".." in name:
        return jsonify({"error": "bad name"}), 400
    p = OUT_DIR / name
    if not p.exists():
        return jsonify({"error": "not found"}), 404
    if name.endswith(".mp3"):
        return send_file(str(p), mimetype="audio/mpeg", as_attachment=False, download_name=name)
    if name.endswith(".wav"):
        return send_file(str(p), mimetype="audio/wav", as_attachment=False, download_name=name)
    if name.endswith(".srt"):
        return send_file(str(p), mimetype="application/x-subrip", as_attachment=False, download_name=name)
    return send_file(str(p), as_attachment=False, download_name=name)


@app.route("/api/files/<name>", methods=["DELETE"])
def delete_file(name):
    if not name or "/" in name or "\\" in name or ".." in name:
        return jsonify({"error": "bad name"}), 400
    p = OUT_DIR / name
    if not p.exists():
        return jsonify({"error": "not found"}), 404
    p.unlink()
    return jsonify({"ok": True})


@app.route("/")
def index():
    r = app.send_static_file("index.html")
    r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    r.headers["Pragma"] = "no-cache"
    r.headers["Expires"] = "0"
    return r


@app.route("/static/<path:filename>")
def static_file(filename):
    return app.send_static_file(filename)


# ---------------------------------------------------------------------------
# single-page UI
# ---------------------------------------------------------------------------
def _build_static():
    return r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>free-tts</title>
<style>
  :root{
    --bg:#0a1128; --panel:#152040; --line:#2a3a5a; --txt:#e8e6df;
    --muted:#8a94a6; --accent:#c8553d; --accent2:#1a365d; --accent3:#25407a; --ok:#4ade80; --bad:#f87171; --fg:var(--txt);
    --border:#c8553d; --hover:#1a2a4d; --shadow:rgba(0,0,0,.6);
  }
  @media (prefers-color-scheme: light){
    :root{
      --bg:#f0f4ff; --panel:#dde3f5; --line:#a0b0d0; --txt:#1a1a2e;
      --muted:#6a7080; --accent:#c8553d; --accent2:#7a8fc9; --accent3:#a0b0d0;
      --ok:#4ade80; --bad:#f87171; --fg:var(--txt);
      --border:#c8553d; --hover:#c8d0e8; --shadow:rgba(0,0,0,.1);
    }
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0;background:var(--bg);color:var(--txt);
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
  body{
    background-color:var(--bg);
    background-image:
      radial-gradient(ellipse at 20% 50%, rgba(93,157,255,.06) 0%, transparent 60%),
      radial-gradient(ellipse at 80% 20%, rgba(194,84,61,.04) 0%, transparent 50%),
      radial-gradient(ellipse at 50% 80%, rgba(160,106,58,.04) 0%, transparent 50%);
  }
  button,input,select,textarea{font:inherit;color:inherit}
  button{cursor:pointer}
  .wrap{max-width:920px;margin:0 auto;padding:24px 16px 80px;
    border:2px solid var(--border);border-radius:16px;
    background:linear-gradient(180deg,var(--hover) 0%,var(--bg) 100%);
    box-shadow:0 4px 24px var(--shadow)}
  h1{font-size:32px;margin:0 0 8px;letter-spacing:.3px;color:var(--txt);font-weight:bold}
  .sub{color:var(--muted);margin-bottom:24px;font-size:16px}
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  .grow{flex:1 1 auto;min-width:160px}
  label{display:block;font-size:12px;color:var(--muted);margin:6px 0 4px}
  input[type=text],select,textarea{
    width:100%;background:var(--panel);border:1px solid var(--line);border-radius:8px;
    padding:8px 10px;outline:none;transition:border-color .12s}
  input[type=text]:focus,select:focus,textarea:focus{border-color:var(--accent)}
  textarea{resize:vertical;min-height:64px}
  .btn{
    background:var(--accent);color:#fff;border:none;border-radius:8px;
    padding:8px 12px;font-weight:600;letter-spacing:.2px;transition:filter .12s,background .12s}
  .btn:hover{filter:brightness(1.08)}
  .btn.ghost{background:transparent;border:1px solid var(--line);color:var(--txt)}
  .btn.ghost:hover{border-color:var(--accent);color:var(--accent)}
  .btn.danger{background:var(--accent2);color:#fff}
  .btn[disabled]{opacity:.5;cursor:not-allowed}
  .panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px;margin-top:14px}
  .row.stacked{gap:10px}
  .status{font-size:13px;color:var(--muted);margin-top:10px;min-height:18px}
  .status.ok{color:var(--ok)} .status.bad{color:var(--bad)}
  .audiobar{margin-top:12px}
  .audiobar audio{width:100%;height:42px;border-radius:8px;background:var(--panel)}
  .actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
  .hint{font-size:12px;color:var(--muted)}
  .hidden{display:none!important}
  #log{font-size:12px;color:var(--muted);margin-top:10px;max-height:120px;overflow:auto;
    background:var(--hover);border:1px solid var(--line);border-radius:8px;padding:6px 8px;white-space:pre-wrap;word-break:break-word}
  .filelist{display:flex;flex-direction:column;gap:6px;margin-top:8px}
  .fileitem{display:flex;gap:8px;align-items:center;justify-content:space-between;
    background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 8px;font-size:13px}
  .fileitem a{color:var(--accent);text-decoration:none}
  .fileitem a:hover{text-decoration:underline}
  .recording{display:none;align-items:center;gap:8px;color:var(--accent2);font-weight:600;margin-top:8px}
  .recording.on{display:flex}
  .dot{width:10px;height:10px;border-radius:50%;background:var(--accent2);animation:pulse 1s infinite}
  @keyframes pulse{0%{opacity:1}50%{opacity:.2}100%{opacity:1}}
  .progress{display:none;align-items:center;gap:8px;margin-top:8px}
  .progress.on{display:flex}
  .spinner{width:14px;height:14px;border:2px solid var(--line);border-top-color:var(--accent);
    border-radius:50%;animation:spin .7s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  .captions{border:1px solid var(--line);border-radius:8px;background:var(--panel);
    margin-top:10px;max-height:200px;overflow:auto;font-size:13px}
  .cap-entry{padding:4px 8px;border-bottom:1px solid var(--line)}
  .cap-entry:last-child{border-bottom:none}
  .cap-entry .cap-time{color:var(--muted);font-size:11px;margin-bottom:2px}
  .cap-entry .cap-text{color:var(--fg)}
  .cap-entry.active{background:var(--accent3);border-left:3px solid var(--accent);padding-left:5px;margin-left:-8px}
</style>
</head>
<body>
<div class="wrap">
  <h1>free-tts</h1>
  <div class="sub">local TTS + voice cloning · save MP3 · play in browser · CLI: python tts.py …</div>

  <div class="row stacked">
    <div class="grow">
      <label for="text">Text</label>
      <textarea id="text" placeholder="Type or paste text here…">Hello world, this is a quick test of free-tts.</textarea>
    </div>
  </div>

  <div class="row stacked">
    <div style="flex:2 1 auto;min-width:200px">
      <label for="engine">Engine</label>
      <select id="engine">
        <option value="auto">auto (kokoro, then clone if ref)</option>
        <option value="kokoro">kokoro</option>
        <option value="coqui-clone">coqui-clone (voice cloning)</option>
        <option value="espeak-ng">espeak-ng</option>
      </select>
    </div>
    <div style="flex:2 1 auto;min-width:160px">
      <label for="voice">Voice (kokoro)</label>
      <select id="voice"></select>
    </div>
    <div style="flex:1 1 auto;min-width:120px">
      <label for="lang">Language</label>
      <select id="lang">
        <option value="en">en</option>
        <option value="en-us">en-us</option>
        <option value="fr">fr</option>
        <option value="de">de</option>
        <option value="es">es</option>
        <option value="it">it</option>
        <option value="pt">pt</option>
        <option value="ja">ja</option>
        <option value="zh">zh</option>
      </select>
    </div>
  </div>

  <div class="panel">
    <div class="row stacked">
      <div>
        <label id="refLabel">Reference audio (for cloning)</label>
        <div class="row">
          <input type="text" id="refPath" placeholder="none — record or upload below" disabled style="flex:2 1 auto" />
          <button class="btn ghost" id="uploadBtn">Upload</button>
          <input type="file" id="uploadInput" accept=".wav,.mp3,.mp4,.webm,.ogg,.flac" class="hidden" />
        </div>
      </div>
    </div>

    <div class="row" style="margin-top:8px">
      <button class="btn ghost" id="recordBtn">Record</button>
    </div>
    <div class="recording" id="recording">
      <span class="dot"></span> Recording… speak clearly, then press Stop.
      <button class="btn danger" id="stopRecBtn" style="margin-left:8px">Stop</button>
    </div>
    <div class="progress" id="progress">
      <span class="spinner"></span><span id="progressText">Generating…</span>
    </div>
    <div class="hint" id="recHint">Allow microphone when prompted. The recording is saved locally and used as the voice-cloning reference.</div>
    <div class="row" style="margin-top:6px;align-items:center;gap:8px">
      <input type="checkbox" id="cleanRef" style="accent-color:var(--accent)" />
      <label for="cleanRef" style="font-size:12px;color:var(--muted);margin:0">Clean audio (denoise + normalize + trim silence) — takes a few seconds</label>
    </div>
  </div>

  <div class="row" style="margin-top:14px">
    <button class="btn" id="runBtn">Generate</button>
    <span class="status" id="status"></span>
  </div>

  <div class="panel" style="margin-top:14px">
    <div class="row" style="margin-bottom:8px">
      <div style="font-size:13px;color:var(--muted)">Generate captions (SRT) for an existing audio file</div>
    </div>
    <div class="row stacked">
      <div>
        <label for="alignFile">Audio file</label>
        <select id="alignFile"><option value="">Pick a file…</option></select>
      </div>
    </div>
    <div class="row stacked" style="margin-top:8px">
      <div style="font-size:12px;color:var(--muted);margin-bottom:4px">Optional text for word alignment</div>
      <textarea id="alignText" placeholder="Leave empty to auto-transcribe…"></textarea>
    </div>
    <div class="row" style="margin-top:8px;gap:8px;align-items:center">
      <input type="checkbox" id="alignDiarize" style="accent-color:var(--accent)" />
      <label for="alignDiarize" style="font-size:12px;color:var(--muted);margin:0">Diarize (speaker labels)</label>
    </div>
    <div class="row" style="margin-top:10px">
      <button class="btn" id="alignBtn">Generate SRT</button>
      <span class="status" id="alignStatus"></span>
    </div>
  </div>

  <div class="audiobar hidden" id="audiobar">
    <audio id="player" controls></audio>
    <div class="actions">
      <a id="downloadLink" class="btn ghost" download>Download MP3</a>
      <button class="btn ghost" id="playBtn">Play</button>
      <button class="btn ghost" id="stopBtn">Stop</button>
      <button class="btn ghost" id="clearBtn">Clear</button>
    </div>
    <div class="captions" id="captions">
      <div class="captions-header">
        <span style="font-size:12px;color:var(--muted)">Captions</span>
      </div>
      <div class="captions-body" id="captionsBody">
        <div class="cap-empty">No captions yet</div>
      </div>
    </div>
  </div>

  <div class="panel">
    <div class="row">
      <div style="font-size:13px;color:var(--muted)">Recent files</div>
      <button class="btn ghost" id="refreshFilesBtn" style="margin-left:auto">Refresh</button>
    </div>
    <div class="filelist" id="filelist"></div>
  </div>

  <div id="log"></div>
</div>

<script>
(function(){
  const $=s=>document.querySelector(s);
  const OUT_DIR='/api/files';
  const log=document.getElementById('log');
  function L(m){log.textContent=(log.textContent+'\n'+m).slice(-2500);log.scrollTop=log.scrollHeight;}
  async function api(path,opts){
    const r=await fetch(path,opts);
    const j=r.text();
    const p=j.then(t=>{try{return JSON.parse(t)}catch(e){return {error:'response not JSON',body:t}}});
    return p.then(x=>{
      if(x.error==='response not JSON'){
        L('Server error (not JSON): '+(x.body||'').slice(0,600));
        return x;
      }
      return x;
    })
  }

  const engine=$('#engine'),voice=$('#voice'),lang=$('#lang'),
    text=$('#text'),status=$('#status'),refPath=$('#refPath'),
    runBtn=$('#runBtn'),recording=$('#recording'),recHint=$('#recHint'),
    recordBtn=$('#recordBtn'),stopRecBtn=$('#stopRecBtn'),
    uploadBtn=$('#uploadBtn'),uploadInput=$('#uploadInput'),
    progress=$('#progress'),progressText=$('#progressText'),
    audiobar=$('#audiobar'),player=$('#player'),downloadLink=$('#downloadLink'),
    playBtn=$('#playBtn'),stopBtn=$('#stopBtn'),clearBtn=$('#clearBtn'),
    filelist=$('#filelist'),refreshFilesBtn=$('#refreshFilesBtn');
  const alignFile=$('#alignFile'),alignDiarize=$('#alignDiarize'),
    alignBtn=$('#alignBtn'),alignStatus=$('#alignStatus'),alignText=$('#alignText');

  let mediaRecorder=null, recordedChunks=[], recordedBlob=null;

  // voice dropdown
  let voices=[];
  voice.innerHTML='<option value="af_sarah">loading voices…</option>';
  (async ()=>{
    try{
      const j=await api('/api/voices');
      if(j.voices&&j.voices.length){voices=j.voices;
        voice.innerHTML=voices.map(v=>`<option value="${v}">${v}</option>`).join('')}
      else{voice.innerHTML='<option value="af_sarah">af_sarah (default)</option>'}
    }catch(e){L('voice probe failed: '+e); voice.innerHTML='<option value="af_sarah">af_sarah</option>'}
  })();

  function setStatus(m,ok){status.textContent=m;status.className='status'+(ok?' ok':(m?' bad':''))}
  function setRunning(on){
    runBtn.disabled=on;recordBtn.disabled=on;uploadBtn.disabled=on;
    engine.disabled=on;lang.disabled=on;text.disabled=on}
  function setProgress(on,txt){
    progress.classList.toggle('on',on); progressText.textContent=txt||''}

  // ---- reference recording ----
  recordBtn.addEventListener('click',async ()=>{
    if(!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia){
      setStatus('Microphone API not available in this browser.','');return}
    try{
      const stream=await navigator.mediaDevices.getUserMedia({audio:true});
      const mime=MediaRecorder.isTypeSupported('audio/webm;codecs=opus')? 'audio/webm;codecs=opus':
                 MediaRecorder.isTypeSupported('audio/webm')? 'audio/webm':
                 MediaRecorder.isTypeSupported('audio/ogg')? 'audio/ogg':'audio/mp3';
      mediaRecorder=new MediaRecorder(stream, mime&&{mimeType:mime});
      recordedChunks=[]; recordedBlob=null;
      mediaRecorder.ondataavailable=e=>{if(e.data.size>0)recordedChunks.push(e.data)};
      mediaRecorder.onstop=()=>{
        recordedBlob=new Blob(recordedChunks,{type:mime||'audio/webm'});
        recording.classList.remove('on');stream.getTracks().forEach(t=>t.stop());
        L('Recording saved locally. Uploading to server…')
        const reader=new FileReader();
        reader.onload=()=>api('/api/record',{
          method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({audio_b64:reader.result.split(',',2)[1]||reader.result, clean: $('#cleanRef').checked})}).then(j=>{
            if(j.path){const name=(j.path.split('\\').pop()||j.path).split('/').pop()||j.path;refPath.value=name;
              L('Reference saved: '+name);setStatus('Reference ready. Generate to use it.','ok')}
            else L('Reference save failed: '+(j.error||'?'))
          }).catch(e=>L('Reference upload error: '+e));
        reader.readAsDataURL(recordedBlob);
      };
      mediaRecorder.start();
      recording.classList.add('on');recHint.textContent='Recording… speak now, then press Stop.';
      L('Recording started (format: '+mime+'). Press Stop when done.')
    }catch(e){
      L('Record error: '+e);setStatus('Microphone error.','');recording.classList.remove('on')}
  });
  stopRecBtn.addEventListener('click',()=>{
    if(mediaRecorder&&mediaRecorder.state!=='inactive'){
      mediaRecorder.stop();
      recHint.textContent='Saving…';
      setTimeout(()=>{if(!recordedBlob){L('No recorded blob.');recHint.textContent='Recording failed.'}},300)
    }
  });

  // ---- upload reference ----
  uploadBtn.addEventListener('click',()=>uploadInput.click());
  uploadInput.addEventListener('change',async ()=>{
    const f=uploadInput.files[0]; if(!f)return;
    const fd=new FormData(); fd.append('file',f);
    if($('#cleanRef').checked) fd.append('clean','1');
    try{
      setProgress(true,'Uploading…');
      const j=await api('/api/upload',{method:'POST',body:fd});
      setProgress(false,'');
      if(j.path){const name=(j.path.split('\\').pop()||j.path).split('/').pop()||j.path;refPath.value=name;
        L('Reference uploaded: '+name);setStatus('Reference ready: '+name,'ok')}
      else setStatus('Upload failed.','')
    }catch(e){setProgress(false,'');setStatus('Upload error.','')}
    uploadInput.value=''});

  // ---- generate ----
  runBtn.addEventListener('click',async ()=>{
    const t=text.value.trim();
    if(!t){setStatus('Enter some text.','');return}
    const e=engine.value, v=voice.value, l=lang.value;
    const ref=refPath.value? (OUT_DIR+'/'+refPath.value) : null;
    setStatus('Generating…','');setRunning(true);setProgress(true,'Generating…');
    audiobar.classList.add('hidden');
    try{
      const body={text:t,engine:e,voice:v,lang:l};
      if(e==='coqui-clone'&&ref)body.ref=ref;
      setProgress(true,'Generating…');
      const j=await fetch('/api/generate',{method:'POST',
        headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      const r=await j.json();
      setProgress(false,'');
      if(!j.ok){L('Generate error: '+(r.error||'?')+'\\n'+(r.stderr||''));setStatus('Error.','')}
      else{
        const name=r.file.name;
        L('Generated: '+name+' ('+r.file.size+' bytes)');
        setStatus('Done — '+name,'ok');
        player.src='/api/files/'+encodeURIComponent(name);
        downloadLink.href='/api/files/'+encodeURIComponent(name);
        downloadLink.download=name;
        audiobar.classList.remove('hidden');
        // Caption SRT download link
        let srtName;
        if(r.captions&&r.captions.path){
          srtName=r.captions.name;
          const capLink=document.createElement('a');
          capLink.href='/api/files/'+encodeURIComponent(srtName);
          capLink.download=srtName;
          capLink.textContent='Download captions (.srt)';
          capLink.className='btn ghost';
          L('Captions: '+srtName+' ('+r.captions.size+' bytes) — import to CapCut/subtitle apps');
          const actions=document.querySelector('.actions');
          if(actions){actions.appendChild(capLink)}
        }
        refreshFileList()

        // Fetch and render caption timeline
        if(r.captions&&r.captions.path){
          try{
            const srtRes=await fetch('/api/files/'+encodeURIComponent(srtName));
            const srtText=await srtRes.text();
            const entries=parseSRT(srtText);
            renderTimeline(entries);
            L('Timeline: '+entries.length+' caption(s) shown');
          }catch(e){L('Timeline fetch error: '+e)}
        }
      }
    }catch(e){setProgress(false,'');L('Generate exception: '+e);setStatus('Error.','')}
    setRunning(false)
  });

  // ---- align existing file ----
  alignBtn.addEventListener('click',async ()=>{
    const fn=alignFile.value;
    if(!fn){alignStatus.textContent='Pick a file.';return}
    const txt=alignText.value.trim();
    const d=alignDiarize.checked;
    alignStatus.textContent='Aligning…';
    try{
      const j=await api('/api/align',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({file:fn,text:txt,diarize:d})});
      if(j.captions){
        L('Aligned SRT: '+j.captions.name+' ('+j.captions.size+' bytes)');
        setStatus('SRT: '+j.captions.name,'ok');
        alignStatus.textContent='Done';
        if(j.captions.fallback)L('Note: text-weighted fallback used')
      }else{
        L('Align error: '+(j.error||'?'));
        alignStatus.textContent='Failed'
      }
    }catch(e){
      L('Align exception: '+e);
      alignStatus.textContent='Failed'
    }
    refreshFileList()
  });

  playBtn.addEventListener('click',()=>player.play().catch(e=>L('Play error: '+e)));
  stopBtn.addEventListener('click',()=>{player.pause();player.currentTime=0});
  clearBtn.addEventListener('click',()=>{player.pause();player.src='';audiobar.classList.add('hidden');setStatus('')});

  // ---- caption timeline + synced display ----
  const capBodyEl=document.getElementById('captionsBody');
  function parseSRT(srtText){
    const entries=[];const blocks=srtText.trim().split(/\n\s*\n/);
    for(const block of blocks){
      const lines=block.split('\n').filter(function(l){return l.length>0});
      if(lines.length<3)continue;
      const idx2=parseInt(lines[0],10);
      if(isNaN(idx2))continue;
      const m=lines[1].match(/(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})/);
      if(!m)continue;
      const start=(+m[1])*3600+(+m[2])*60+(+m[3])+(m[4]/1000);
      const end=(+m[5])*3600+(+m[6])*60+(+m[7])+(m[8]/1000);
      entries.push({idx:idx2,start:start,end:end,text:lines.slice(2).join('\n')});
    }
    return entries;
  }
  function fmtDur(s){
    if(s<0)s=0;s=Math.floor(s*100)/100;
    const m=Math.floor((s%3600)/60),sec=(s%60).toFixed(2);
    const p=function(v){return String(v).padStart(2,'0')};
    return(p(m)+':'+p(sec));
  }
  function renderTimeline(entries){
    capBodyEl.innerHTML='';
    if(!entries||!entries.length){capBodyEl.innerHTML='<div class="cap-empty">No captions</div>';return}
    entries.forEach(function(e){
      const div=document.createElement('div');
      div.className='cap-entry';
      div.dataset.start=e.start;div.dataset.end=e.end;
      const t=document.createElement('div');t.className='cap-time';
      t.textContent=fmtDur(e.start)+' \u2192 '+fmtDur(e.end);
      const tx=document.createElement('div');tx.className='cap-text';tx.textContent=e.text;
      div.appendChild(t);div.appendChild(tx);
      capBodyEl.appendChild(div);
    });
  }
  function syncActiveCaption(){
    if(!capBodyEl||!capBodyEl.children.length)return;
    const first=capBodyEl.children[0];
    if(first.classList&&first.classList.contains('cap-empty'))return;
    const ct=player.currentTime||0;
    let active=null;
    Array.from(capBodyEl.children).forEach(function(el){
      const s=+el.dataset.start,e=+el.dataset.end;
      const on=ct>=s&&ct<e;
      el.classList.toggle('active',on);
      if(on)active=el;
    });
    if(active&&capBodyEl.scrollHeight>capBodyEl.clientHeight){
      const sc=active.offsetTop-((capBodyEl.clientHeight||300)/2)+active.clientHeight/2;
      capBodyEl.scrollTop=Math.max(0,sc);
    }
  }
  player.addEventListener('timeupdate',syncActiveCaption);
  player.addEventListener('play',syncActiveCaption);
  player.addEventListener('seeked',syncActiveCaption);

  
  async function refreshFileList(){
    try{
      const res=await fetch('/api/files');const j=await res.json();
      filelist.innerHTML='';
      alignFile.innerHTML='<option value="">Pick a file…</option>';
      if(!j.files||!j.files.length){filelist.innerHTML='<div class="hint">No files yet.</div>';return}
      j.files.filter(f=>f.type==='mp3'||f.type==='wav').forEach(f=>{
        const o=document.createElement('option');o.value=f.name;
        o.textContent=f.name+' ('+(f.size/1024).toFixed(0)+'KB)';alignFile.appendChild(o)
      });
      j.files.slice(0,60).forEach(f=>{
        const div=document.createElement('div');div.className='fileitem';
        const a=document.createElement('a');a.href='/api/files/'+encodeURIComponent(f.name);
        a.textContent=f.name+' ('+f.size+' b)';
        const del=document.createElement('button');del.className='btn ghost';
        del.textContent='Del';del.style.padding='2px 8px';del.style.fontSize='12px';
        del.onclick=async ()=>{
          try{await fetch('/api/files/'+encodeURIComponent(f.name),{method:'DELETE'});
            L('Deleted: '+f.name);await refreshFileList()}catch(e){L('Del error: '+e)}
        };
        div.appendChild(a);div.appendChild(del);filelist.appendChild(div)
      })
    }catch(e){L('file list error: '+e)}
  }
  refreshFilesBtn.addEventListener('click',refreshFileList);
  refreshFileList();

  L('ready. Type text, pick engine/voice, optionally record/upload a reference, then Generate.')
})();
</script>
</body>
</html>
"""


def _ensure_static():
    static_dir = HERE / "static"
    static_dir.mkdir(exist_ok=True)
    (static_dir / "index.html").write_text(_build_static(), encoding="utf-8")


if __name__ == "__main__":
    _ensure_static()
    host = os.environ.get("FREE_TTS_HOST", "127.0.0.1")
    port = int(os.environ.get("FREE_TTS_PORT", "5000"))
    print(f"Starting free-tts-ui on http://{host}:{port}")
    print("CLI still works:  python tts.py \"text\" -o out.mp3")
    app.run(host=host, port=port, debug=False, use_reloader=False)
