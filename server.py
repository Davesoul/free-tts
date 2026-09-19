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

def _write_srt(srt_path, text, audio_size):
    """Write an SRT file from the input text.
    Splits text by line breaks; estimates timing from audio file size
    (192 kbps MP3: ~1 byte per 0.004s) and number of segments.
    Each segment gets equal duration; minimum 2s per caption.
    """
    segments = [s.strip() for s in text.split("\n") if s.strip()]
    if not segments:
        segments = [text.strip()]
    n = len(segments)
    # Estimate total duration from MP3 size (192kbps ~= 24000 bytes/sec, but
    # actual bitrate varies; use 20000 bytes/sec as conservative estimate)
    total_sec = max(audio_size / 20000, n * 2)
    per_seg = total_sec / n
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, 1):
            start = (i - 1) * per_seg
            end = i * per_seg
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
            wav_path = None
            # convert to mp3
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(out_path.with_suffix(".wav")),
                 "-codec:a", "libmp3lame", "-b:a", "192k", str(out_path)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            out_path.with_suffix(".wav").unlink(missing_ok=True)
            info = {"path": str(out_path), "size": out_path.stat().st_size, "name": out_name}
            return jsonify({"ok": True, "file": info})
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
            return jsonify({"ok": True, "file": info})
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
    # Generate SRT captions from the input text
    srt_name = f"tts_{ts}.srt"
    srt_path = OUT_DIR / srt_name
    try:
        _write_srt(srt_path, text, out_path.stat().st_size)
        srt_info = {"path": str(srt_path), "size": srt_path.stat().st_size, "name": srt_name}
    except Exception:
        srt_info = None
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
    clean = f.form.get("clean") or False  # optional bool from multipart form
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


@app.route("/api/files")
def list_files():
    try:
        files = []
        for p in sorted(OUT_DIR.glob("*.mp3"), reverse=True):
            files.append({"name": p.name, "size": p.stat().st_size, "type": "mp3"})
        for p in sorted(OUT_DIR.glob("*.wav"), reverse=True):
            files.append({"name": p.name, "size": p.stat().st_size, "type": "wav"})
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
    --bg:#0e0f12; --panel:#16181d; --line:#262a31; --txt:#e7e9ee;
    --muted:#8a8f99; --accent:#5d9dff; --accent2:#c2543d; --ok:#46c267; --bad:#e24;
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0;background:var(--bg);color:var(--txt);
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
  button,input,select,textarea{font:inherit;color:inherit}
  button{cursor:pointer}
  .wrap{max-width:920px;margin:0 auto;padding:24px 16px 80px}
  h1{font-size:22px;margin:0 0 4px;letter-spacing:.2px}
  .sub{color:var(--muted);margin-bottom:20px}
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  .grow{flex:1 1 auto;min-width:160px}
  label{display:block;font-size:12px;color:var(--muted);margin:6px 0 4px}
  input[type=text],select,textarea{
    width:100%;background:var(--panel);border:1px solid var(--line);border-radius:8px;
    padding:8px 10px;outline:none;transition:border-color .12s}
  input[type=text]:focus,select:focus,textarea:focus{border-color:var(--accent)}
  textarea{resize:vertical;min-height:64px}
  .btn{
    background:var(--accent);color:#0b1016;border:none;border-radius:8px;
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
    background:#0b0d10;border:1px solid var(--line);border-radius:8px;padding:6px 8px;white-space:pre-wrap;word-break:break-word}
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

  <div class="audiobar hidden" id="audiobar">
    <audio id="player" controls></audio>
    <div class="actions">
      <a id="downloadLink" class="btn ghost" download>Download MP3</a>
      <button class="btn ghost" id="playBtn">Play</button>
      <button class="btn ghost" id="stopBtn">Stop</button>
      <button class="btn ghost" id="clearBtn">Clear</button>
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
    const r=fetch(path,opts);
    const j=r.then(res=>res.text());
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
        if(r.captions&&r.captions.path){
          const srtName=r.captions.name;
          const capLink=document.createElement('a');
          capLink.href='/api/files/'+encodeURIComponent(srtName);
          capLink.download=srtName;
          capLink.textContent='Download captions (.srt)';
          capLink.className='btn ghost';
          L('Captions: '+srtName+' ('+r.captions.size+' bytes) — import to CapCut/subtitle apps');
        }
        refreshFileList()
      }
    }catch(e){setProgress(false,'');L('Generate exception: '+e);setStatus('Error.','')}
    setRunning(false)
  });

  playBtn.addEventListener('click',()=>player.play().catch(e=>L('Play error: '+e)));
  stopBtn.addEventListener('click',()=>{player.pause();player.currentTime=0});
  clearBtn.addEventListener('click',()=>{player.pause();player.src='';audiobar.classList.add('hidden');setStatus('')});

  async function refreshFileList(){
    try{
      const res=await fetch('/api/files');const j=await res.json();
      filelist.innerHTML='';
      if(!j.files||!j.files.length){filelist.innerHTML='<div class="hint">No files yet.</div>';return}
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
