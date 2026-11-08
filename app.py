"""
Farman920 Vertical Live Looper — single-file Flask app

Gallery se video select karo -> server par upload hoga -> FFmpeg usse
480x854 vertical me loop karke YouTube RTMP par live chalata rahega.

DEPLOYMENT (Render or another always-on Linux server):
  Build command:  pip install flask gunicorn
  Start command:  gunicorn --workers 1 --threads 4 --timeout 0 app:app

The server must have FFmpeg (and ffprobe) installed and available on PATH.
Long streams ke liye always-on paid service use karein (free tier sleep ho jata hai).

Optional security:
  Set DASHBOARD_PASSWORD in the server environment. Dashboard HTTP Basic
  Authentication use karega. Unprotected RTMP control panel expose na karein.

Note: Render jaise hosts par disk ephemeral hota hai. Server restart hone par
uploaded video delete ho jayegi -> dobara upload karni padegi.
"""

import os
import re
import time
import shutil
import tempfile
import threading
import subprocess
from flask import Flask, request, jsonify, Response

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2 GB upload limit

DEFAULT_HOURS = 100
MAX_HOURS = 720
ALLOWED_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".3gp"}
UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "farman920_uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

_lock = threading.Lock()
_state = {
    "running": False,
    "started_at": None,
    "ends_at": None,
    "error": None,
    "process": None,
    "timer_file": None,
    "monitor": None,
    "video_path": None,
    "video_name": None,
}

HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<meta name="theme-color" content="#f8f5ee">
<title>Farman920 • Live Loop Control</title>
<style>
:root{--bg:#f8f5ee;--card:#fffdf8;--ink:#17251f;--muted:#718078;--green:#16845b;--gold:#c99a43;--line:#e9e4d9}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 85% 0%,#e8f0e7 0,transparent 32%),var(--bg);color:var(--ink);font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif;min-height:100vh}
main{width:min(100% - 28px,520px);margin:0 auto;padding:24px 0 38px}
header{display:flex;align-items:center;justify-content:space-between;margin-bottom:22px}
.brand{display:flex;align-items:center;gap:11px}.logo{width:44px;height:44px;border-radius:16px;background:linear-gradient(145deg,#1c9a6a,#12533f);display:grid;place-items:center;color:white;font-weight:900;box-shadow:0 8px 20px #16845b30}
h1{font-size:18px;margin:0;letter-spacing:-.4px}.sub{font-size:11px;color:var(--muted);margin-top:3px}
.pill{font-size:11px;font-weight:800;letter-spacing:.6px;padding:8px 11px;border-radius:30px;background:#eeeae1;color:#647068}.pill.live{background:#e4f5eb;color:#087647}
h2{font-size:14px;margin:0 0 14px}.card{background:#ffffffc9;border:1px solid #fff;border-radius:25px;padding:19px;margin:13px 0;box-shadow:0 12px 35px #273a2c0b}
.preview{position:relative;overflow:hidden;border-radius:25px;background:linear-gradient(150deg,#0b1e19,#102d25 60%,#16251f);color:white;aspect-ratio:9/16;max-height:440px;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:25px}
.preview video{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:none;z-index:0}
.preview.hasvideo video{display:block}
.preview.hasvideo .glow,.preview.hasvideo:before,.preview.hasvideo:after,.preview.hasvideo .ph{display:none}
.preview:before,.preview:after{content:"";position:absolute;border:1px solid #ffffff16;border-radius:50%;width:280px;height:280px;animation:orbit 14s linear infinite}.preview:after{width:360px;height:360px;animation-duration:21s;animation-direction:reverse}
.glow{position:absolute;width:170px;height:170px;border-radius:50%;background:#1a9a6b35;filter:blur(40px);animation:pulse 4s ease-in-out infinite}
.ph{z-index:1}.live-tag{color:#ffb5a8;font-size:10px;font-weight:900;letter-spacing:2px;margin-bottom:18px}.ph-title{font-size:20px;font-weight:900}.ph-sub{font-size:11px;color:#a7c8b7;margin-top:8px;letter-spacing:1px}
.overlay{position:absolute;z-index:2;left:0;right:0;bottom:18px;text-align:center;text-shadow:0 2px 10px #000a}
.digits{font-size:clamp(28px,8vw,44px);font-weight:900;letter-spacing:-2px;font-variant-numeric:tabular-nums}
.preview-brand{font-size:12px;font-weight:800;letter-spacing:2px;margin-top:4px}
@keyframes orbit{to{transform:rotate(360deg)}}@keyframes pulse{50%{transform:scale(1.35);opacity:.55}}
.field{display:block;font-size:11px;color:#66756d;font-weight:800;margin:13px 0 7px;letter-spacing:.3px}
input[type=text],input[type=password],input[type=number]{width:100%;border:1px solid var(--line);border-radius:15px;padding:14px;background:#fffefa;color:var(--ink);font:14px inherit;outline:none}
input:focus{border-color:#70b99a;box-shadow:0 0 0 3px #16845b13}
input[type=file]{display:none}
button,.pick{border:0;border-radius:17px;padding:16px;width:100%;font-weight:900;font-size:14px;cursor:pointer;transition:transform .2s,filter .2s;display:block;text-align:center}
button:active,.pick:active{transform:scale(.98)}
.pick{background:#eef6f1;color:#0d6848;border:1.5px dashed #70b99a}
.start{background:linear-gradient(110deg,#16845b,#0d6848);color:white;box-shadow:0 10px 22px #16845b30;margin-top:16px}
.start:disabled{opacity:.45;cursor:not-allowed}
.stop{background:#fff0ed;color:#b83c2b;margin-top:10px}.stop:disabled{opacity:.4;cursor:not-allowed}
.note{font-size:11px;color:var(--muted);line-height:1.55;margin:12px 2px 0}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}.stat{background:#f7f5ef;border-radius:16px;padding:12px}.stat b{display:block;font-size:13px}.stat span{font-size:10px;color:var(--muted)}
.profile{display:flex;justify-content:space-between;gap:12px;align-items:center;padding:10px 0;border-bottom:1px solid var(--line);font-size:12px}.profile:last-child{border:0}.profile span{color:var(--muted)}.profile b{text-align:right;overflow-wrap:anywhere}
.bar{height:8px;background:#e9e4d9;border-radius:8px;margin-top:12px;overflow:hidden;display:none}.bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,#16845b,#54dca4);transition:width .15s}
.row{display:flex;align-items:center;gap:10px;margin-top:14px;font-size:12px;color:#44524a}.row input{width:18px;height:18px}
#fname{font-size:12px;color:var(--muted);margin-top:10px;overflow-wrap:anywhere}
#message{font-size:12px;line-height:1.5;color:var(--muted);margin-top:10px;min-height:18px}
footer{text-align:center;color:#89958d;font-size:10px;margin-top:20px;letter-spacing:.5px}
</style>
</head>
<body><main>
<header><div class="brand"><div class="logo">F</div><div><h1>FARMAN920 LIVE</h1><div class="sub">VIDEO LOOP BROADCAST CONTROL</div></div></div><div id="status" class="pill">● OFFLINE</div></header>

<section class="preview" id="preview">
<video id="pv" muted loop playsinline></video>
<div class="glow"></div>
<div class="ph"><div class="live-tag">● LIVE LOOP</div><div class="ph-title">Gallery se video chuniye</div><div class="ph-sub">VIDEO LOOP HOGI AUTOMATIC</div></div>
<div class="overlay" id="ovl" style="display:none"><div class="digits" id="countdown">100:00:00</div><div class="preview-brand">FARMAN920</div></div>
</section>

<div class="stats"><div class="stat"><b>480p Vertical</b><span>480 × 854 · 30 FPS</span></div><div class="stat"><b id="durStat">100 Hours</b><span>Loop till timer ends</span></div></div>

<section class="card">
<h2>1. Video (Gallery)</h2>
<label class="pick" for="file">📁 GALLERY SE VIDEO CHUNEIN</label>
<input id="file" type="file" accept="video/*">
<div class="bar" id="bar"><i id="barfill"></i></div>
<div id="fname">Koi video select nahi hui.</div>
</section>

<section class="card">
<h2>2. YouTube RTMP Connection</h2>
<label class="field" for="rtmp">RTMP SERVER URL</label>
<input id="rtmp" type="text" value="rtmp://a.rtmp.youtube.com/live2" autocomplete="url">
<label class="field" for="key">YOUTUBE STREAM KEY</label>
<input id="key" type="password" placeholder="Paste your YouTube stream key" autocomplete="off">
<label class="field" for="hours">STREAM DURATION (HOURS)</label>
<input id="hours" type="number" min="1" max="720" value="100">
<label class="row"><input type="checkbox" id="showtimer" checked> Video par countdown timer dikhayein</label>
<button class="start" id="start" onclick="startStream()" disabled>START LOOP LIVE</button>
<button class="stop" id="stop" onclick="stopStream()" disabled>STOP STREAM</button>
<div id="message">Pehle video upload karein, phir stream key daal kar start karein. Key private rakhein.</div>
</section>

<section class="card"><h2>Channel & Creator</h2>
<div class="profile"><span>Streaming channel</span><b>@UNLIMITED_16K</b></div>
<div class="profile"><span>Creator brand</span><b>Farman920</b></div>
<div class="profile"><span>Instagram</span><b>@IGFARMAN</b></div>
<div class="profile"><span>UPI ID</span><b>farman920@ybl</b></div>
<p class="note">Aapki chuni hui video server par upload hoti hai aur cloud se loop me stream hoti hai, isliye stream shuru hone ke baad phone band/lock kar sakte hain. UPI sirf profile info hai; live video me embed nahi hai.</p>
</section>
<footer>MADE FOR FARMAN920 · CLOUD LOOP SESSION</footer>
</main>
<script>
let localEnd=null, uploaded=false, isRunning=false, uploading=false;
const $=id=>document.getElementById(id);
function fmt(s){s=Math.max(0,Math.floor(s));let h=Math.floor(s/3600),m=Math.floor(s%3600/60),v=s%60;return [h,m,v].map(n=>String(n).padStart(2,'0')).join(':')}
function paint(){if(localEnd){$('countdown').textContent=fmt(Math.max(0,localEnd-Date.now()/1000))}}
function updateButtons(){$('start').disabled=!(uploaded&&!isRunning&&!uploading);$('stop').disabled=!isRunning}
$('hours').addEventListener('input',()=>{let h=parseInt($('hours').value)||0;$('durStat').textContent=h+' Hours';if(!isRunning)$('countdown').textContent=fmt(h*3600)});

async function refresh(){
 try{
  let r=await fetch('/status'),d=await r.json();
  isRunning=d.running;
  $('status').textContent=d.running?'● LIVE':'● OFFLINE';
  $('status').className='pill'+(d.running?' live':'');
  if(d.video_name&&!uploading){uploaded=true;$('fname').textContent='Server par ready: '+d.video_name}
  if(d.running&&d.ends_at){localEnd=d.ends_at;$('ovl').style.display='block';paint()}
  else{localEnd=null;if(d.error)$('countdown').textContent='ERROR'}
  if(d.error)$('message').textContent='Server: '+d.error;
  updateButtons();
 }catch(e){$('message').textContent='Cannot reach streaming server.'}
}

$('file').addEventListener('change',()=>{
 const f=$('file').files[0]; if(!f)return;
 // local preview
 const pv=$('pv'); pv.src=URL.createObjectURL(f); pv.play().catch(()=>{});
 $('preview').classList.add('hasvideo');
 $('ovl').style.display='block';
 $('countdown').textContent=fmt((parseInt($('hours').value)||0)*3600);
 // upload
 uploaded=false; uploading=true; updateButtons();
 const fd=new FormData(); fd.append('video',f);
 const xhr=new XMLHttpRequest();
 xhr.open('POST','/upload');
 $('bar').style.display='block'; $('barfill').style.width='0';
 $('fname').textContent='Uploading: '+f.name;
 xhr.upload.onprogress=e=>{if(e.lengthComputable){let p=Math.round(e.loaded/e.total*100);$('barfill').style.width=p+'%';$('message').textContent='Upload ho raha hai… '+p+'%'}};
 xhr.onload=()=>{
  uploading=false;
  let d={};try{d=JSON.parse(xhr.responseText)}catch(e){}
  if(xhr.status===200){uploaded=true;$('fname').textContent='Server par ready: '+(d.name||f.name);$('message').textContent='Upload complete ✅ Ab stream key daal kar START dabayein.'}
  else{uploaded=false;$('message').textContent=d.error||'Upload failed.'}
  updateButtons();
 };
 xhr.onerror=()=>{uploading=false;uploaded=false;$('message').textContent='Upload failed (network).';updateButtons()};
 xhr.send(fd);
});

async function startStream(){
 let rtmp=$('rtmp').value.trim(),key=$('key').value.trim(),hours=parseFloat($('hours').value)||0;
 if(!/^rtmps?:\/\//i.test(rtmp)||!key){$('message').textContent='Valid RTMP URL aur stream key dono enter karein.';return}
 if(hours<=0){$('message').textContent='Duration sahi daalein.';return}
 $('start').disabled=true;$('message').textContent='Starting cloud encoder…';
 try{
  let r=await fetch('/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rtmp,key,hours,overlay:$('showtimer').checked})}),d=await r.json();
  $('message').textContent=d.message||d.error||'Request complete';
  await refresh();
 }catch(e){$('message').textContent='Start request failed.';updateButtons()}
}
async function stopStream(){
 if(!confirm('Live stream stop karna hai?'))return;
 let r=await fetch('/stop',{method:'POST'});let d=await r.json();
 $('message').textContent=d.message||d.error;await refresh();
}
setInterval(paint,250);setInterval(refresh,3000);refresh();
</script></body></html>"""


def _auth_ok():
    password = os.environ.get("DASHBOARD_PASSWORD")
    if not password:
        return True
    auth = request.authorization
    return bool(auth and auth.password == password)


@app.before_request
def protect_dashboard():
    if not _auth_ok():
        return Response("Authentication required", 401,
                        {"WWW-Authenticate": 'Basic realm="Farman920 Live"'})


def _write_countdown(path, end_time, proc):
    while proc.poll() is None:
        remaining = max(0, int(end_time - time.time()))
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(f"{remaining // 3600:02d}:{(remaining % 3600) // 60:02d}:{remaining % 60:02d}")
            os.replace(tmp, path)  # atomic, so FFmpeg never reads a half-written file
        except OSError:
            break
        if remaining <= 0:
            break
        time.sleep(1)


def _terminate(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()


def _stop_process():
    with _lock:
        proc = _state.get("process")
        _state["running"] = False
        _state["process"] = None
        path = _state.get("timer_file")
        _state["timer_file"] = None
    _terminate(proc)
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def _monitor_process(proc, timer_path, end_time):
    while proc.poll() is None and time.time() < end_time:
        time.sleep(2)
    _terminate(proc)
    err = None
    if proc.returncode not in (0, None) and time.time() < end_time - 5:
        # Stopped early and not by user request
        with _lock:
            by_user = _state.get("process") is not proc
        if not by_user:
            err = f"FFmpeg stopped (exit code {proc.returncode}). Check RTMP URL/key and server FFmpeg logs."
    with _lock:
        if _state.get("process") is proc:
            _state["running"] = False
            _state["process"] = None
            _state["error"] = err
            _state["timer_file"] = None
    try:
        os.unlink(timer_path)
    except OSError:
        pass


def _has_audio(path):
    """True if the video has an audio track (checked with ffprobe)."""
    if not shutil.which("ffprobe"):
        return False
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30)
        return bool(out.stdout.strip())
    except Exception:
        return False


@app.get("/")
def index():
    return HTML


@app.get("/status")
def status():
    with _lock:
        running = bool(_state["running"] and _state["process"] and _state["process"].poll() is None)
        return jsonify(running=running, started_at=_state["started_at"],
                       ends_at=_state["ends_at"], error=_state["error"],
                       video_name=_state["video_name"])


@app.post("/upload")
def upload():
    with _lock:
        proc = _state.get("process")
        if proc and proc.poll() is None:
            return jsonify(error="Stream chal rahi hai. Pehle stop karein, phir nayi video upload karein."), 409

    f = request.files.get("video")
    if not f or not f.filename:
        return jsonify(error="Koi video file nahi mili."), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_EXT:
        return jsonify(error="Unsupported format. MP4/MOV/MKV/WEBM use karein."), 400

    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(f.filename))[:80] or "video" + ext
    new_path = os.path.join(UPLOAD_DIR, f"loop_{int(time.time())}{ext}")
    try:
        f.save(new_path)
    except Exception as exc:
        try:
            os.unlink(new_path)
        except OSError:
            pass
        return jsonify(error=f"Upload save failed: {exc}"), 500

    with _lock:
        old = _state.get("video_path")
        _state["video_path"] = new_path
        _state["video_name"] = safe_name
        _state["error"] = None
    if old and old != new_path:
        try:
            os.unlink(old)
        except OSError:
            pass
    return jsonify(message="Upload complete.", name=safe_name)


@app.post("/start")
def start():
    if not shutil.which("ffmpeg"):
        return jsonify(error="FFmpeg is not installed on this server. Install FFmpeg and restart."), 500
    data = request.get_json(silent=True) or {}
    rtmp = str(data.get("rtmp", "")).strip()
    key = str(data.get("key", "")).strip()
    overlay = bool(data.get("overlay", True))
    try:
        hours = float(data.get("hours", DEFAULT_HOURS))
    except (TypeError, ValueError):
        hours = DEFAULT_HOURS
    if not (0 < hours <= MAX_HOURS):
        return jsonify(error=f"Duration 0 se {MAX_HOURS} hours ke beech honi chahiye."), 400
    duration = int(hours * 3600)

    if not re.match(r"^rtmps?://", rtmp, re.I) or not key or any(c.isspace() for c in key):
        return jsonify(error="Valid RTMP URL aur stream key enter karein."), 400
    if len(key) > 300 or len(rtmp) > 500:
        return jsonify(error="RTMP URL or stream key is too long."), 400

    with _lock:
        old = _state.get("process")
        if old and old.poll() is None:
            return jsonify(message="A stream is already running."), 409
        video_path = _state.get("video_path")

    if not video_path or not os.path.exists(video_path):
        return jsonify(error="Pehle gallery se video upload karein."), 400

    timer_path = None
    end_time = time.time() + duration

    # --- Video filter: fill 480x854 vertical frame (center crop), 30 fps ---
    vf = ("scale=480:854:force_original_aspect_ratio=increase,"
          "crop=480:854,setsar=1,fps=30")

    if overlay:
        timer_file = tempfile.NamedTemporaryFile(mode="w", prefix="farm920_", suffix=".txt",
                                                  delete=False, encoding="utf-8")
        timer_path = timer_file.name
        timer_file.write(f"{duration // 3600:02d}:{(duration % 3600) // 60:02d}:{duration % 60:02d}")
        timer_file.close()
        vf += (
            f",drawtext=textfile={timer_path}:reload=1:fontcolor=white:fontsize=44:"
            "box=1:boxcolor=0x00000066:boxborderw=10:"
            "x=(w-text_w)/2:y=h*0.84,"
            "drawtext=text='FARMAN920':fontcolor=0x9ce5c1:fontsize=22:"
            "box=1:boxcolor=0x00000066:boxborderw=6:"
            "x=(w-text_w)/2:y=h*0.91"
        )

    output_url = rtmp.rstrip("/") + "/" + key

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning",
           # -stream_loop -1 = video ko infinite loop me chalao
           "-re", "-stream_loop", "-1", "-i", video_path]

    if _has_audio(video_path):
        audio_map = ["-map", "0:v:0", "-map", "0:a:0"]
    else:
        # Video me audio nahi hai -> silent audio (YouTube ko audio track chahiye)
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
        audio_map = ["-map", "0:v:0", "-map", "1:a:0"]

    cmd += audio_map + [
        "-t", str(duration),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        "-pix_fmt", "yuv420p", "-r", "30", "-g", "60", "-keyint_min", "60",
        "-b:v", "1200k", "-maxrate", "1400k", "-bufsize", "2400k",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        "-f", "flv", output_url,
    ]

    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
        if proc.poll() is not None:
            if timer_path:
                try:
                    os.unlink(timer_path)
                except OSError:
                    pass
            return jsonify(error="FFmpeg could not connect. Check the RTMP URL, stream key, and video format."), 502
    except Exception as exc:
        if timer_path:
            try:
                os.unlink(timer_path)
            except OSError:
                pass
        return jsonify(error=f"Could not launch FFmpeg: {exc}"), 500

    with _lock:
        _state.update(running=True, started_at=int(time.time()),
                      ends_at=int(end_time), error=None, process=proc,
                      timer_file=timer_path)

    if timer_path:
        threading.Thread(target=_write_countdown, args=(timer_path, end_time, proc),
                         daemon=True).start()
    monitor = threading.Thread(target=_monitor_process,
                               args=(proc, timer_path or "", end_time), daemon=True)
    with _lock:
        _state["monitor"] = monitor
    monitor.start()
    return jsonify(message="Stream started. Video loop cloud server se chal rahi hai.",
                   ends_at=int(end_time))


@app.post("/stop")
def stop():
    with _lock:
        proc = _state.get("process")
        if not proc or proc.poll() is not None:
            _state["running"] = False
            return jsonify(message="No active stream.")
    _stop_process()
    return jsonify(message="Stream stopped.")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), threaded=True)
