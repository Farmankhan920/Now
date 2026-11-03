"""
Farman920 Vertical Live Timer — single-file Flask app

DEPLOYMENT (Render or another always-on Linux server):
  Build command:  pip install flask gunicorn
  Start command:  gunicorn --workers 1 --threads 4 --timeout 0 app:app

The server must have FFmpeg installed and available on PATH.
On Render, use an always-on paid service/worker for long streams. Free web
services can sleep/stop and are not suitable for a 100-hour broadcast.

Optional security:
  Set DASHBOARD_PASSWORD in the server environment. The dashboard then uses
  HTTP Basic Authentication. Do not expose an unprotected RTMP control panel.

This app generates a vertical 480x854 animated slate (not your phone screen),
with silent audio, and pushes it to the YouTube RTMP ingest server.
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

DURATION_SECONDS = 100 * 60 * 60
YOUTUBE_RTMP = "rtmp://a.rtmp.youtube.com/live2"

_lock = threading.Lock()
_state = {
    "running": False,
    "started_at": None,
    "ends_at": None,
    "error": None,
    "process": None,
    "timer_file": None,
    "monitor": None,
}

HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<meta name="theme-color" content="#f8f5ee">
<title>Farman920 • Live Control</title>
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
.preview:before,.preview:after{content:"";position:absolute;border:1px solid #ffffff16;border-radius:50%;width:280px;height:280px;animation:orbit 14s linear infinite}.preview:after{width:360px;height:360px;animation-duration:21s;animation-direction:reverse}
.glow{position:absolute;width:170px;height:170px;border-radius:50%;background:#1a9a6b35;filter:blur(40px);animation:pulse 4s ease-in-out infinite}
.live-tag{z-index:1;color:#ffb5a8;font-size:10px;font-weight:900;letter-spacing:2px;margin-bottom:22px}.digits{z-index:1;font-size:clamp(36px,10vw,58px);font-weight:900;letter-spacing:-3px;font-variant-numeric:tabular-nums;text-shadow:0 0 25px #54dca466}
.preview-label{z-index:1;font-size:10px;letter-spacing:3px;color:#a7c8b7;margin-top:10px}.preview-brand{z-index:1;margin-top:36px;font-size:13px;font-weight:800;letter-spacing:2px}.preview-handle{z-index:1;color:#8eafa0;font-size:10px;margin-top:7px}
@keyframes orbit{to{transform:rotate(360deg)}}@keyframes pulse{50%{transform:scale(1.35);opacity:.55}}
.field{display:block;font-size:11px;color:#66756d;font-weight:800;margin:13px 0 7px;letter-spacing:.3px}
input{width:100%;border:1px solid var(--line);border-radius:15px;padding:14px;background:#fffefa;color:var(--ink);font:14px inherit;outline:none}
input:focus{border-color:#70b99a;box-shadow:0 0 0 3px #16845b13}
button{border:0;border-radius:17px;padding:16px;width:100%;font-weight:900;font-size:14px;cursor:pointer;transition:transform .2s,filter .2s}
button:active{transform:scale(.98)}.start{background:linear-gradient(110deg,#16845b,#0d6848);color:white;box-shadow:0 10px 22px #16845b30;margin-top:16px}
.stop{background:#fff0ed;color:#b83c2b;margin-top:10px}.stop:disabled{opacity:.4;cursor:not-allowed}
.note{font-size:11px;color:var(--muted);line-height:1.55;margin:12px 2px 0}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}.stat{background:#f7f5ef;border-radius:16px;padding:12px}.stat b{display:block;font-size:13px}.stat span{font-size:10px;color:var(--muted)}
.profile{display:flex;justify-content:space-between;gap:12px;align-items:center;padding:10px 0;border-bottom:1px solid var(--line);font-size:12px}.profile:last-child{border:0}.profile span{color:var(--muted)}.profile b{text-align:right;overflow-wrap:anywhere}
#message{font-size:12px;line-height:1.5;color:var(--muted);margin-top:10px;min-height:18px}
footer{text-align:center;color:#89958d;font-size:10px;margin-top:20px;letter-spacing:.5px}
</style>
</head>
<body><main>
<header><div class="brand"><div class="logo">F</div><div><h1>FARMAN920 LIVE</h1><div class="sub">VERTICAL BROADCAST CONTROL</div></div></div><div id="status" class="pill">● OFFLINE</div></header>
<section class="preview">
<div class="glow"></div><div class="live-tag">● LIVE BROADCAST</div>
<div class="digits" id="countdown">100:00:00</div><div class="preview-label">HOURS · MINUTES · SECONDS</div>
<div class="preview-brand">FARMAN920</div><div class="preview-handle">@UNLIMITED_16K · IGFARMAN</div>
</section>
<div class="stats"><div class="stat"><b>480p Vertical</b><span>480 × 854 · 30 FPS</span></div><div class="stat"><b>100 Hours</b><span>Cloud-side countdown</span></div></div>
<section class="card">
<h2>YouTube RTMP Connection</h2>
<label class="field" for="rtmp">RTMP SERVER URL</label>
<input id="rtmp" value="rtmp://a.rtmp.youtube.com/live2" autocomplete="url">
<label class="field" for="key">YOUTUBE STREAM KEY</label>
<input id="key" type="password" placeholder="Paste your YouTube stream key" autocomplete="off">
<button class="start" id="start" onclick="startStream()">START 100-HOUR LIVE</button>
<button class="stop" id="stop" onclick="stopStream()" disabled>STOP STREAM</button>
<div id="message">Enter your YouTube stream key, then start. Keep this key private.</div>
</section>
<section class="card"><h2>Channel & Creator</h2>
<div class="profile"><span>Streaming channel</span><b>@UNLIMITED_16K</b></div>
<div class="profile"><span>Creator brand</span><b>Farman920</b></div>
<div class="profile"><span>Instagram</span><b>@IGFARMAN</b></div>
<div class="profile"><span>UPI ID</span><b>farman920@ybl</b></div>
<p class="note">The live video is an animated timer screen generated by the cloud server. It does not capture your phone display or game. UPI is shown here as profile information only; it is not embedded in the live video.</p>
</section>
<footer>MADE FOR FARMAN920 · CLOUD STREAM SESSION</footer>
</main>
<script>
let localEnd=null, poller=null;
const $=id=>document.getElementById(id);
function fmt(s){s=Math.max(0,Math.floor(s));let h=Math.floor(s/3600),m=Math.floor(s%3600/60),v=s%60;return [h,m,v].map(n=>String(n).padStart(2,'0')).join(':')}
function paint(){if(localEnd){let left=Math.max(0,Math.floor(localEnd-Date.now()/1000));$('countdown').textContent=fmt(left)}}
async function refresh(){try{let r=await fetch('/status'),d=await r.json();$('status').textContent=d.running?'● LIVE':'● OFFLINE';$('status').className='pill'+(d.running?' live':'');$('start').disabled=d.running;$('stop').disabled=!d.running;if(d.running&&d.ends_at){localEnd=d.ends_at;paint()}else{localEnd=null;$('countdown').textContent=d.error?'ERROR':'100:00:00'}if(d.error)$('message').textContent='Server: '+d.error}catch(e){$('message').textContent='Cannot reach streaming server.'}}
async function startStream(){let rtmp=$('rtmp').value.trim(),key=$('key').value.trim();if(!/^rtmps?:\/\//i.test(rtmp)||!key){$('message').textContent='Valid RTMP URL aur stream key dono enter karein.';return}
$('start').disabled=true;$('message').textContent='Starting cloud encoder…';
try{let r=await fetch('/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rtmp,key})}),d=await r.json();$('message').textContent=d.message||d.error||'Request complete';await refresh()}catch(e){$('message').textContent='Start request failed.';$('start').disabled=false}}
async function stopStream(){if(!confirm('Live stream stop karna hai?'))return;let r=await fetch('/stop',{method:'POST'});let d=await r.json();$('message').textContent=d.message||d.error;await refresh()}
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


def _write_countdown(path, end_time):
    while True:
        remaining = max(0, int(end_time - time.time()))
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"{remaining // 3600:02d}:{(remaining % 3600) // 60:02d}:{remaining % 60:02d}")
        if remaining <= 0:
            break
        time.sleep(1)


def _stop_process():
    with _lock:
        proc = _state.get("process")
        timer = _state.get("monitor")
        _state["running"] = False
        _state["process"] = None
        path = _state.get("timer_file")
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def _monitor_process(proc, timer_path, end_time):
    while proc.poll() is None and time.time() < end_time:
        time.sleep(2)
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
    err = None
    if proc.returncode not in (0, None):
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


@app.get("/")
def index():
    return HTML


@app.get("/status")
def status():
    with _lock:
        running = bool(_state["running"] and _state["process"] and _state["process"].poll() is None)
        return jsonify(running=running, started_at=_state["started_at"],
                       ends_at=_state["ends_at"], error=_state["error"])


@app.post("/start")
def start():
    if not shutil.which("ffmpeg"):
        return jsonify(error="FFmpeg is not installed on this server. Install FFmpeg and restart."), 500
    data = request.get_json(silent=True) or {}
    rtmp = str(data.get("rtmp", "")).strip()
    key = str(data.get("key", "")).strip()
    if not re.match(r"^rtmps?://", rtmp, re.I) or not key or any(c.isspace() for c in key):
        return jsonify(error="Valid RTMP URL aur stream key enter karein."), 400
    if len(key) > 300 or len(rtmp) > 500:
        return jsonify(error="RTMP URL or stream key is too long."), 400

    with _lock:
        old = _state.get("process")
        if old and old.poll() is None:
            return jsonify(message="A stream is already running."), 409

    timer_file = tempfile.NamedTemporaryFile(mode="w", prefix="farm920_", suffix=".txt",
                                              delete=False, encoding="utf-8")
    timer_path = timer_file.name
    timer_file.write("100:00:00")
    timer_file.close()
    end_time = time.time() + DURATION_SECONDS

    # Video: 480x854 portrait animated background and countdown; audio: silence.
    # The RTMP key is passed as part of the output URL and is never returned by /status.
    video_filter = (
        "drawbox=x='(w-110)/2+90*sin(t*0.7)':y='150+35*cos(t*0.9)':"
        "w=110:h=3:color=0x31d69a@0.65:t=fill,"
        f"drawtext=textfile={timer_path}:reload=1:fontcolor=white:fontsize=58:"
        "x=(w-text_w)/2:y=(h-text_h)/2-25,"
        "drawtext=text='FARMAN920':fontcolor=0x9ce5c1:fontsize=27:"
        "x=(w-text_w)/2:y=h*0.68,"
        "drawtext=text='@UNLIMITED_16K  |  IGFARMAN':fontcolor=0xb0c9bc:fontsize=14:"
        "x=(w-text_w)/2:y=h*0.73"
    )
    output_url = rtmp.rstrip("/") + "/" + key
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-re", "-f", "lavfi", "-i", "color=c=0x0b1e19:s=480x854:r=30",
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-t", str(DURATION_SECONDS),
        "-vf", video_filter,
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        "-pix_fmt", "yuv420p", "-r", "30", "-g", "60",
        "-b:v", "800k", "-maxrate", "900k", "-bufsize", "1600k",
        "-c:a", "aac", "-b:a", "96k", "-ar", "44100",
        "-f", "flv", output_url
    ]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                text=True)
        time.sleep(1.5)
        if proc.poll() is not None:
            os.unlink(timer_path)
            return jsonify(error="FFmpeg could not connect. Check the RTMP URL, stream key, and FFmpeg build."), 502
    except Exception as exc:
        try:
            os.unlink(timer_path)
        except OSError:
            pass
        return jsonify(error=f"Could not launch FFmpeg: {exc}"), 500

    with _lock:
        _state.update(running=True, started_at=int(time.time()),
                      ends_at=int(end_time), error=None, process=proc,
                      timer_file=timer_path)
    threading.Thread(target=_write_countdown, args=(timer_path, end_time),
                     daemon=True).start()
    monitor = threading.Thread(target=_monitor_process,
                               args=(proc, timer_path, end_time), daemon=True)
    with _lock:
        _state["monitor"] = monitor
    monitor.start()
    return jsonify(message="Stream started. Countdown is running on the cloud server.",
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
