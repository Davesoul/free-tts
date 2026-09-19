#!/usr/bin/env python3
"""
free-tts — simple local TTS, saves to MP3. Engines:
  kokoro      — Kokoro TTS (neural, natural, Apache 2.0)
  coqui-clone — Coqui XTTS v2 (zero-shot voice cloning from a reference clip)
  espeak-ng   — espeak-ng (fast, robotic, no ML)
  pyttsx3     — system TTS (lowest quality, broadest compatibility)

First available engine is used when no -e is given; for clone use -e coqui-clone.
"""

import argparse
import subprocess
import sys
from tempfile import NamedTemporaryFile
from pathlib import Path

# ---------------------------------------------------------------------------
# MP3 export — ffmpeg is on PATH; pydub is not needed (works on Py3.14)
# ---------------------------------------------------------------------------
def _wav_to_mp3(wav_path: Path, mp3_path: Path, bitrate: str = "192k") -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(wav_path),
            "-codec:a",
            "libmp3lame",
            "-b:a",
            bitrate,
            str(mp3_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return mp3_path


# ---------------------------------------------------------------------------
# Kokoro (primary, neural TTS) — lazy import
# ---------------------------------------------------------------------------
_KOKORO_IMPORTED = False
_KOKORO_READY = False
_KOKORO_PIPELINE = None
_KOKORO_ERROR = None


def _import_kokoro():
    global _KOKORO_IMPORTED, _KOKORO_ERROR, KokoroPipeline, PipelineConfig, GenerationConfig
    if _KOKORO_IMPORTED:
        return
    try:
        from pykokoro import KokoroPipeline, PipelineConfig
        from pykokoro.generation_config import GenerationConfig
        _KOKORO_IMPORTED = True
    except Exception as e:
        _KOKORO_ERROR = str(e)
        KokoroPipeline = PipelineConfig = GenerationConfig = None

def _ensure_kokoro():
    global _KOKORO_PIPELINE, _KOKORO_READY
    if not _KOKORO_READY:
        from pykokoro import PipelineConfig as _PC
        if not _KOKORO_PIPELINE:
            _KOKORO_PIPELINE = KokoroPipeline(_PC())
        _KOKORO_READY = True


def generate_kokoro(text: str, voice: str, lang: str, output_mp3: Path) -> Path:
    global _KOKORO_PIPELINE, _KOKORO_READY

    if not _KOKORO_IMPORTED:
        _import_kokoro()
    if not _KOKORO_IMPORTED:
        print(f"Error: kokoro requested but unavailable ({_KOKORO_ERROR})", file=sys.stderr)
        return None  # caller handles
    if not _KOKORO_READY:
        _ensure_kokoro()
        from pykokoro import PipelineConfig as _PC
        from pykokoro.generation_config import GenerationConfig
        _KOKORO_PIPELINE = KokoroPipeline(
            _PC(voice=voice, generation=GenerationConfig(lang=lang))
        )

    res = _KOKORO_PIPELINE.run(text)
    audio = res.audio  # numpy float32
    sample_rate = res.sample_rate

    wav_path = output_mp3.with_suffix(".wav")
    import soundfile as sf

    sf.write(str(wav_path), audio, sample_rate)
    _wav_to_mp3(wav_path, output_mp3)
    wav_path.unlink(missing_ok=True)
    return output_mp3


# ---------------------------------------------------------------------------
# Coqui XTTS v2 — zero-shot voice cloning — lazy import
# ---------------------------------------------------------------------------
_COQUI_IMPORTED = False
_COQUI_READY = False
_COQUI_TTS = None
_COQUI_ERROR = None


def _import_coqui():
    global _COQUI_IMPORTED, _COQUI_ERROR, TTS
    if _COQUI_IMPORTED:
        return
    try:
        from TTS.api import TTS
        _COQUI_IMPORTED = True
    except Exception as e:
        _COQUI_ERROR = str(e)
        TTS = None


def _get_coqui_tts():
    global _COQUI_TTS, _COQUI_READY
    if not _COQUI_READY:
        _COQUI_TTS = TTS("tts_models/multilingual/multi-dataset/xtts_v2", progress_bar=False)
        _COQUI_READY = True
    return _COQUI_TTS


def generate_coqui_clone(text: str, ref_audio: Path, lang: str, output_mp3: Path) -> Path:
    if not _COQUI_IMPORTED:
        _import_coqui()
    if not _COQUI_IMPORTED:
        print(f"Error: coqui-clone requested but unavailable ({_COQUI_ERROR})", file=sys.stderr)
        return None
    tts = _get_coqui_tts()

    wav_path = output_mp3.with_suffix(".wav")
    tts.tts_to_file(
        text=text,
        speaker_wav=str(ref_audio),
        language=lang,
        file_path=str(wav_path),
    )
    _wav_to_mp3(wav_path, output_mp3)
    wav_path.unlink(missing_ok=True)
    return output_mp3


# ---------------------------------------------------------------------------
# espeak-ng fallback
# ---------------------------------------------------------------------------
try:
    import espeakng_loader

    _ESPEAK_READY = True
except Exception:
    _ESPEAK_READY = False


def generate_espeak(text: str, output_mp3: Path) -> Path:
    with NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = Path(tmp.name)

    subprocess.run(
        ["espeak-ng", "-w", str(wav_path), text],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wav_to_mp3(wav_path, output_mp3)
    wav_path.unlink(missing_ok=True)
    return output_mp3


# ---------------------------------------------------------------------------
# pyttsx3 fallback
# ---------------------------------------------------------------------------
try:
    import pyttsx3

    _PYTTSX3_ENGINE = None
    _PYTTSX3_READY = False
except Exception:
    _PYTTSX3_ENGINE = None
    _PYTTSX3_READY = False


def generate_pyttsx3(text: str, output_mp3: Path) -> Path:
    global _PYTTSX3_ENGINE

    if _PYTTSX3_ENGINE is None:
        _PYTTSX3_ENGINE = pyttsx3.init()

    with NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = Path(tmp.name)

    _PYTTSX3_ENGINE.save_to_file(text, str(wav_path))
    _PYTTSX3_ENGINE.runAndWait()
    _wav_to_mp3(wav_path, output_mp3)
    wav_path.unlink(missing_ok=True)
    return output_mp3


# ---------------------------------------------------------------------------
# Engine picker
# ---------------------------------------------------------------------------
def get_available_engines() -> list[tuple[str, bool]]:
    _import_kokoro()
    _import_coqui()
    return [
        ("kokoro", _KOKORO_IMPORTED),
        ("coqui-clone", _COQUI_IMPORTED),
        ("espeak-ng", _ESPEAK_READY),
        ("pyttsx3", _PYTTSX3_READY),
    ]


# ---------------------------------------------------------------------------
# Main CLI
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="free-tts: generate speech to MP3, fully local, free."
    )
    parser.add_argument("text", nargs="?", help="Text to speak (or use -f)")
    parser.add_argument("-o", "--output", default="output.mp3", help="Output MP3 path")
    parser.add_argument("-e", "--engine", choices=["kokoro", "coqui-clone", "espeak-ng", "pyttsx3"], help="TTS engine")
    parser.add_argument("-v", "--voice", default="af_sarah", help="Kokoro voice name")
    parser.add_argument("-l", "--lang", default="en", help="Language code (default en; use xx-xx for others)")
    parser.add_argument("-r", "--ref", help="Reference audio WAV for coqui-clone voice cloning")
    parser.add_argument("-f", "--file", help="Read text from file")
    parser.add_argument("--list-engines", action="store_true", help="List engines available on this system")
    parser.add_argument("--check", action="store_true", help="Quick import sanity check, no synthesis")

    args = parser.parse_args()

    if args.check:
        print("Import check:")
        for name, ready in get_available_engines():
            print(f"  {name:12s}  {'READY' if ready else 'MISSING'}")
        return 0

    if args.list_engines:
        print("Available engines:")
        for name, ready in get_available_engines():
            print(f"  {name:12s}  {'READY' if ready else 'NOT AVAILABLE'}")
        return 0

    # Resolve text
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8").strip()
    elif args.text:
        text = args.text
    else:
        parser.error("Provide text or use -f/--file")

    if not text:
        print("Error: no text to speak.", file=sys.stderr)
        return 1

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    # Trigger lazy engine imports so the module-level flags are populated
    # before we check them in the dispatch below. (--check / --list-engines
    # also call get_available_engines(), but the synthesis paths do not.)
    _import_kokoro()
    _import_coqui()
    engine = args.engine
    if not engine:
        # auto-pick: clone if ref given, else kokoro, else coqui, then fallbacks
        if args.ref and _COQUI_IMPORTED:
            engine = "coqui-clone"
        elif _KOKORO_IMPORTED:
            engine = "kokoro"
        elif _COQUI_IMPORTED:
            engine = "coqui-clone"
        elif _ESPEAK_READY:
            engine = "espeak-ng"
        elif _PYTTSX3_READY:
            engine = "pyttsx3"
        else:
            print("Error: no TTS engine available.", file=sys.stderr)
            return 1

    print(f"Engine : {engine}")
    if engine == "coqui-clone":
        if not args.ref:
            parser.error("coqui-clone requires -r/--ref reference audio")
        ref = Path(args.ref)
        if not ref.exists():
            print(f"Error: reference audio not found: {ref}", file=sys.stderr)
            return 1
        print(f"Ref    : {ref}")
    print(f"Text   : {text[:80]}{'...' if len(text) > 80 else ''}")
    print(f"Output : {output.resolve()}")

    try:
        if engine == "kokoro":
            if not _KOKORO_IMPORTED:
                print(f"Error: kokoro requested but unavailable ({_KOKORO_ERROR})", file=sys.stderr)
                return 1
            generate_kokoro(text, args.voice, args.lang, output)
        elif engine == "coqui-clone":
            if not _COQUI_IMPORTED:
                print(f"Error: coqui-clone requested but unavailable ({_COQUI_ERROR})", file=sys.stderr)
                return 1
            generate_coqui_clone(text, Path(args.ref), args.lang, output)
        elif engine == "espeak-ng":
            if not _ESPEAK_READY:
                print("Error: espeak-ng requested but not available.", file=sys.stderr)
                return 1
            generate_espeak(text, output)
        elif engine == "pyttsx3":
            if not _PYTTSX3_READY:
                print("Error: pyttsx3 requested but not available.", file=sys.stderr)
                return 1
            generate_pyttsx3(text, output)
        else:
            print(f"Error: unknown engine {engine}", file=sys.stderr)
            return 1
    except subprocess.CalledProcessError as e:
        print(f"Error: ffmpeg conversion failed: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error during generation: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1

    size_kb = output.stat().st_size / 1024
    print(f"Done. Saved {size_kb:.0f} KB to {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
