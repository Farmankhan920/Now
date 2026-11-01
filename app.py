"""
Stream Deck: restream a YouTube video/live or a Google Drive video to any RTMP(S) target
(YouTube, Facebook, Twitch, custom) with FFmpeg -c copy. UI + backend in this one file.

Env vars:
  APP_PASSWORD  protect the page with a login (strongly recommended)
  APP_USER      login username (default: admin)
  YT_COOKIES    optional: contents of a Netscape cookies.txt (or paste it in the UI)
  YT_PROXY      optional: proxy URL used only to reach YouTube
  KEEPALIVE_URL optional: public URL of this app (auto-detected on Render/Koyeb)
"""
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.request
from collections import deque
from urllib.parse import urlparse

import yt_dlp
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

APP_USER = os.environ.get("APP_USER", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD")

ENV_COOKIES = os.environ.get("YT_COOKIES", "")


class NeedCookies(ValueError):
    """YouTube blocked the server's IP and wants a logged-in session."""

lock = threading.Lock()
state = {
    "job": None, "started": None, "exit_code": None, "title": "", "source": "",
    "phase": "", "restarts": 0, "duration": 0, "progress": "",
    "logs": deque(maxlen=60),
}

YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
DRIVE_HOSTS = {"drive.google.com", "docs.google.com"}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"


# ---------------------------------------------------------------- auth
@app.before_request
def require_login():
    if not APP_PASSWORD or request.path == "/health":
        return None
    a = request.authorization
    if a and secrets.compare_digest((a.username or "").encode(), APP_USER.encode()) \
            and secrets.compare_digest((a.password or "").encode(), APP_PASSWORD.encode()):
        return None
    return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Stream Deck"'})


# ---------------------------------------------------------------- source resolving
def detect_source(url):
    host = (urlparse(url).hostname or "").lower()
    if host in YT_HOSTS:
        return "youtube"
    if host in DRIVE_HOSTS:
        return "drive"
    raise ValueError("Paste a YouTube or Google Drive link.")


def resolve_drive(url):
    m = re.search(r"/d/([\w-]+)", url) or re.search(r"[?&]id=([\w-]+)", url)
    if not m:
        raise ValueError("Couldn't find a file ID in that Drive link.")
    direct = ("https://drive.usercontent.google.com/download"
              f"?id={m.group(1)}&export=download&confirm=t")
    return {"urls": [direct], "live": False, "ua": UA, "title": "Google Drive video",
            "duration": probe_duration(direct)}


def cookies_to_netscape(raw):
    """Accept Netscape cookies.txt or Cookie-Editor style JSON; return Netscape text."""
    raw = (raw or "").replace("\r\n", "\n").strip()
    if not raw:
        return ""
    if raw[0] in "[{":
        try:
            data = json.loads(raw)
        except ValueError:
            raise ValueError("Cookies look like JSON but couldn't be read. "
                             "Export again and paste the whole text.")
        if isinstance(data, dict):
            data = data.get("cookies", [])
        far = int(time.time()) + 365 * 24 * 3600
        lines = ["# Netscape HTTP Cookie File"]
        for c in data:
            try:
                domain = c["domain"]
                host_only = bool(c.get("hostOnly", not domain.startswith(".")))
                if not host_only and not domain.startswith("."):
                    domain = "." + domain
                exp = c.get("expirationDate")
                exp = int(exp) if exp and not c.get("session") else far
                lines.append("\t".join([
                    ("#HttpOnly_" if c.get("httpOnly") else "") + domain,
                    "FALSE" if host_only else "TRUE",
                    c.get("path") or "/",
                    "TRUE" if c.get("secure") else "FALSE",
                    str(exp), c["name"], str(c.get("value", "")),
                ]))
            except (KeyError, TypeError, AttributeError):
                continue
        if len(lines) == 1:
            raise ValueError("No usable cookies found in what you pasted.")
        return "\n".join(lines)
    if not raw.startswith(("# Netscape", "# HTTP Cookie")):
        raw = "# Netscape HTTP Cookie File\n" + raw
    return raw


def resolve_youtube(url, quality, cookie_text=""):
    cap = {"best": "", "1080": "[height<=1080]", "720": "[height<=720]", "480": "[height<=480]"}.get(quality, "")
    # H.264 + AAC only, because FLV with -c copy accepts nothing else.
    fmt = (f"bv*[vcodec^=avc1]{cap}+ba[acodec^=mp4a]/b[vcodec^=avc1]{cap}"
           "/bv*[vcodec^=avc1]+ba[acodec^=mp4a]/b[vcodec^=avc1]/b")
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "skip_download": True, "format": fmt}
    if os.environ.get("YT_PROXY"):
        opts["proxy"] = os.environ["YT_PROXY"]

    cookie_path = None
    text = cookies_to_netscape(cookie_text or ENV_COOKIES or "")
    if text:
        fd, cookie_path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w") as f:
            f.write(text + "\n")
        opts["cookiefile"] = cookie_path

    info, last_err = None, None
    try:
        # Try several YouTube player clients; some get past the bot check.
        for client in (None, "tv", "android_vr", "web_safari", "mweb"):
            o = dict(opts)
            if client:
                o["extractor_args"] = {"youtube": {"player_client": [client]}}
            try:
                with yt_dlp.YoutubeDL(o) as ydl:
                    info = ydl.extract_info(url, download=False)
                break
            except yt_dlp.utils.DownloadError as e:
                last_err = e
    finally:
        if cookie_path:
            try:
                os.remove(cookie_path)
            except OSError:
                pass

    if info is None:
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(last_err)).replace("ERROR: ", "")
        if "not a bot" in msg or "Sign in" in msg:
            raise NeedCookies("YouTube blocked this server. Paste your YouTube cookies in "
                              "the YouTube access box below, then press Run again.")
        raise ValueError(f"YouTube said no: {msg[:220]}")

    fmts = info.get("requested_formats") or [info]
    vcodec = (fmts[0].get("vcodec") or "")
    if vcodec not in ("", "none") and not vcodec.startswith(("avc1", "h264")):
        raise ValueError(f"This video only has {vcodec.split('.')[0]} video. "
                         "-c copy needs H.264; try a lower quality.")
    headers = info.get("http_headers") or {}
    return {"urls": [f["url"] for f in fmts], "live": bool(info.get("is_live")),
            "ua": headers.get("User-Agent", UA), "title": info.get("title") or "YouTube video",
            "duration": 0 if info.get("is_live") else int(info.get("duration") or 0)}


# ---------------------------------------------------------------- ffmpeg supervisor
TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
STALL_SECONDS = 60   # no media progress for this long -> restart ffmpeg
MAX_FAILS = 30       # consecutive failed attempts before giving up
OUT_FAIL_LIMIT = 8   # consecutive 'destination refused us' failures before giving up


def is_running():
    j = state["job"]
    return bool(j and j["active"])


def log(msg):
    state["logs"].append(msg)


def hms(sec):
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def probe_duration(url):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-user_agent", UA, "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", url],
            capture_output=True, text=True, timeout=25).stdout.strip()
        return int(float(out))
    except Exception:
        return 0


def diagnose(output):
    """Say why the destination can't be reached: DNS, TCP, TLS, or the server itself refusing."""
    p = urlparse(output)
    host, port = p.hostname, p.port
    try:
        ip = socket.gethostbyname(host)
    except OSError as e:
        return f"[check] DNS lookup for {host} failed ({e}). The stream URL looks wrong."
    try:
        sock = socket.create_connection((host, port), timeout=8)
    except OSError as e:
        return f"[check] {host} resolved ({ip}) but port {port} is unreachable ({e})."
    if p.scheme != "rtmps":
        sock.close()
        return f"[check] DNS and TCP to {host}:{port} are fine, so the server itself refused the stream."
    try:
        with ssl.create_default_context().wrap_socket(sock, server_hostname=host) as t:
            ver = t.version()
        return (f"[check] DNS, TCP and TLS ({ver}) to {host}:{port} all work, so the platform "
                "itself is refusing the stream. Usually the key or URL is expired or wrong, or "
                "the platform isn't waiting for a stream yet.")
    except (ssl.SSLError, OSError) as e:
        return f"[check] TLS handshake with {host}:{port} failed ({e})."


def normalize_stream_url(url):
    """Make the ingest URL explicit: scheme://host:port/path (IVS hosts need :443 and /app)."""
    p = urlparse(url)
    host = p.hostname or ""
    port = p.port or (443 if p.scheme == "rtmps" else 1935)
    path = p.path.rstrip("/")
    if not path and host.endswith("live-video.net"):
        path = "/app"
    return f"{p.scheme}://{host}:{port}{path}"


TEST_SRC = {"test": True, "urls": [], "live": True, "ua": UA,
            "title": "Test pattern (30 seconds)", "duration": 0}


def build_cmd(src, output, loop, start_at=0.0):
    if src.get("test"):  # small colour-bars + tone, to check the destination accepts us
        return ["ffmpeg", "-hide_banner", "-loglevel", "info",
                "-re", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=15",
                "-re", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                "-t", "30", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-g", "30", "-pix_fmt", "yuv420p", "-b:v", "600k",
                "-c:a", "aac", "-b:a", "96k", "-ar", "44100", "-f", "flv", output]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "info"]
    for url in src["urls"]:
        cmd += ["-user_agent", src["ua"],
                "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
        if start_at > 0 and not src["live"]:
            cmd += ["-ss", f"{start_at:.2f}"]
        if loop and not src["live"]:
            cmd += ["-stream_loop", "-1"]
        if not src["live"]:
            cmd += ["-re"]
        cmd += ["-i", url]
    if len(src["urls"]) == 2:
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
    cmd += ["-c", "copy", "-flvflags", "no_duration_filesize", "-f", "flv", output]
    return cmd


def resolve_source(job):
    if job["kind"] == "test":
        return dict(TEST_SRC)
    if job["kind"] == "drive":
        return resolve_drive(job["video_url"])
    return resolve_youtube(job["video_url"], job["quality"], job["cookies"])


def pump_logs(job, proc, base, run):
    for line in proc.stderr:
        line = line.strip()
        if not line:
            continue
        line = line.replace(job["key"], "****")[:300]
        m = TIME_RE.search(line)
        if m:
            t = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
            job["pos"] = base + t
            if t > run["last_t"]:
                run["last_t"], run["beat"] = t, time.time()
            state["progress"] = line
        else:
            if "Error opening output" in line:
                run["out_fail"] = True
            log(line)


def nap(job, seconds):
    for _ in range(int(seconds)):
        if job["stop"]:
            return
        time.sleep(1)


def resume_point(job, src):
    if src["live"] or job["pos"] < 5:
        return 0.0
    pos = job["pos"] - 3  # replay a few seconds so nothing is skipped
    dur = src.get("duration") or 0
    if job["loop"]:
        return (pos % dur) if dur else 0.0
    return max(0.0, pos)


def supervise(job, src):
    """Run ffmpeg and keep it running: if it drops or stalls, resume where it left off."""
    fails, out_fails, first, rc = 0, 0, True, None
    try:
        while not job["stop"]:
            if not first:
                state["phase"] = "reconnecting"
                try:
                    src = resolve_source(job)
                except ValueError as e:
                    fails += 1
                    log(f"[could not refresh the video link: {e}]")
                    if fails > MAX_FAILS:
                        break
                    nap(job, min(5 + 3 * fails, 30))
                    continue
            first = False
            dur = src.get("duration") or 0
            state["duration"] = dur
            start_at = resume_point(job, src)
            cmd = build_cmd(src, job["output"], job["loop"], start_at)
            run = {"last_t": -1.0, "beat": time.time()}
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                errors="replace", bufsize=1, start_new_session=True)
            job["proc"] = proc
            state["phase"] = "streaming"
            reader = threading.Thread(target=pump_logs, args=(job, proc, start_at, run), daemon=True)
            reader.start()

            while proc.poll() is None:
                if job["stop"]:
                    break
                if time.time() - run["beat"] > STALL_SECONDS:
                    log(f"[no progress for {STALL_SECONDS}s, restarting ffmpeg]")
                    proc.kill()
                    break
                time.sleep(1)
            proc.wait()
            reader.join(timeout=3)
            rc = proc.returncode
            if job["stop"]:
                break

            if job["loop"]:
                complete = False
            elif src["live"]:
                complete = rc == 0
            elif dur:
                complete = job["pos"] >= dur - 8
            else:
                complete = rc == 0
            if complete:
                log("[finished: the video played to the end]")
                break

            if run.get("out_fail"):
                out_fails += 1
                if out_fails in (1, 4):
                    log(diagnose(job["output"]))
                if out_fails >= OUT_FAIL_LIMIT:
                    log("[the destination keeps refusing the connection, so I stopped. "
                        "Get a fresh URL and key from the platform, wait for it to say it is "
                        "ready, then press Run again]")
                    break
            else:
                out_fails = 0

            fails = 0 if run["last_t"] >= 20 else fails + 1
            if fails > MAX_FAILS:
                log("[giving up after repeated failures]")
                break
            wait = min(2 + 3 * fails, 30)
            state["restarts"] += 1
            log(f"[stream dropped (code {rc}); resuming from {hms(job['pos'])} in {wait}s]")
            nap(job, wait)
    except Exception as e:  # never let the supervisor die silently
        log(f"[supervisor error: {e}]")
    finally:
        job["active"] = False
        state["exit_code"] = rc
        state["phase"] = ""
        state["progress"] = ""
        if job["stop"]:
            log("[stopped]")


def keepalive():
    """Free hosts sleep without inbound traffic; ping our own public URL while streaming."""
    base = os.environ.get("KEEPALIVE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or ""
    if not base and os.environ.get("KOYEB_PUBLIC_DOMAIN"):
        base = "https://" + os.environ["KOYEB_PUBLIC_DOMAIN"]
    if not base:
        return
    while True:
        time.sleep(240)
        if is_running():
            try:
                urllib.request.urlopen(base.rstrip("/") + "/health", timeout=15).read()
            except Exception:
                pass


threading.Thread(target=keepalive, daemon=True).start()


# ---------------------------------------------------------------- routes
@app.get("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.get("/health")
def health():
    return "ok"


@app.post("/start")
def start():
    d = request.get_json(silent=True) or {}
    video_url = (d.get("video_url") or "").strip()
    stream_url = (d.get("stream_url") or "").strip().rstrip("/")
    stream_key = (d.get("stream_key") or "").strip()
    quality = d.get("quality") or "1080"
    loop = bool(d.get("loop"))
    test = bool(d.get("test"))

    if not (stream_url and stream_key and (video_url or test)):
        return jsonify(error="Fill in the video link, stream URL and stream key."), 400
    if not stream_url.startswith(("rtmp://", "rtmps://")):
        return jsonify(error="Stream URL must start with rtmp:// or rtmps://"), 400
    if is_running():
        return jsonify(error="A stream is already running. Stop it first."), 409
    stream_url = normalize_stream_url(stream_url)

    try:
        cookies = d.get("cookies") or ""
        if test:
            kind, src = "test", dict(TEST_SRC)
        else:
            kind = detect_source(video_url)
            src = (resolve_drive(video_url) if kind == "drive"
                   else resolve_youtube(video_url, quality, cookies))
    except NeedCookies as e:
        return jsonify(error=str(e), need_cookies=True), 400
    except ValueError as e:
        return jsonify(error=str(e)), 400

    with lock:
        if is_running():
            return jsonify(error="A stream is already running. Stop it first."), 409
        job = {"active": True, "stop": False, "proc": None, "pos": 0.0, "kind": kind,
               "video_url": video_url, "quality": quality, "cookies": cookies, "loop": loop,
               "output": f"{stream_url}/{stream_key}", "key": stream_key}
        state.update(job=job, started=time.time(), exit_code=None, title=src["title"],
                     source=kind, phase="streaming", restarts=0, progress="",
                     duration=src.get("duration") or 0)
        state["logs"].clear()
        threading.Thread(target=supervise, args=(job, src), daemon=True).start()
    return jsonify(ok=True, title=src["title"], live_source=src["live"])


@app.post("/stop")
def stop():
    job = state["job"]
    if job and job["active"]:
        job["stop"] = True
        proc = job.get("proc")
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
    return jsonify(ok=True)


@app.get("/status")
def status():
    running = is_running()
    job = state["job"]
    logs = list(state["logs"])
    if running and state["progress"]:
        logs.append(state["progress"])
    return jsonify(
        running=running,
        uptime=int(time.time() - state["started"]) if running and state["started"] else 0,
        exit_code=state["exit_code"], title=state["title"], source=state["source"],
        phase=state["phase"], restarts=state["restarts"], duration=state["duration"],
        pos=int(job["pos"]) if job else 0, logs=logs)


# ---------------------------------------------------------------- the whole UI
INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stream Deck</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&family=JetBrains+Mono:wght@400&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0d0820; --card: rgba(255,255,255,.065); --stroke: rgba(255,255,255,.13);
    --text: #f5f2ff; --muted: #a59dc8; --hot: #ff3366; --amber: #ffab2e; --cyan: #36e2ff; --ok: #3ee89a;
    --r-xl: 32px; --r-lg: 22px; --r-md: 16px;
  }
  * { box-sizing: border-box; }
  html { background: var(--bg); }
  body {
    margin: 0; min-height: 100vh; color: var(--text);
    font: 16px/1.5 "Space Grotesk", system-ui, sans-serif;
    background:
      radial-gradient(60rem 36rem at 8% -10%, rgba(255,51,102,.28), transparent 60%),
      radial-gradient(50rem 34rem at 100% 0%, rgba(54,226,255,.18), transparent 60%),
      radial-gradient(46rem 30rem at 50% 115%, rgba(124,77,255,.30), transparent 60%),
      var(--bg);
    background-attachment: fixed;
    padding: 28px 16px 56px;
  }
  .wrap { max-width: 920px; margin: 0 auto; display: grid; gap: 18px; }
  .card {
    background: var(--card); border: 1px solid var(--stroke); border-radius: var(--r-xl);
    padding: 24px; backdrop-filter: blur(18px); -webkit-backdrop-filter: blur(18px);
    box-shadow: 0 24px 60px -24px rgba(0,0,0,.65), inset 0 1px 0 rgba(255,255,255,.08);
  }
  h1, h2, p { margin: 0; }
  h2 { font-size: 1.05rem; font-weight: 700; margin-bottom: 4px; }
  .sub { color: var(--muted); font-size: .9rem; margin-bottom: 18px; }

  /* top bar */
  .top { display: flex; align-items: center; gap: 12px; padding: 0 6px; }
  .logo { width: 44px; height: 44px; border-radius: 15px; display: grid; place-items: center;
    background: linear-gradient(135deg, var(--hot), var(--amber)); box-shadow: 0 10px 28px -8px var(--hot); }
  .top h1 { font-size: 1.4rem; font-weight: 700; letter-spacing: -.02em; }
  .top span { color: var(--muted); font-size: .85rem; display: block; margin-top: -2px; }

  /* hero */
  .hero { display: grid; grid-template-columns: auto 1fr; gap: 28px; align-items: center; overflow: hidden; position: relative; }
  .ring { width: 188px; height: 188px; position: relative; display: grid; place-items: center; }
  .ring::before {
    content: ""; position: absolute; inset: 0; border-radius: 50%; padding: 7px;
    background: conic-gradient(from 0deg, var(--hot), var(--amber), var(--cyan), var(--hot));
    -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
    -webkit-mask-composite: xor; mask-composite: exclude; opacity: .22; transition: opacity .4s;
  }
  .live .ring::before { opacity: 1; animation: spin 3.2s linear infinite; }
  .live .ring { filter: drop-shadow(0 0 22px rgba(255,51,102,.55)); }
  @keyframes spin { to { transform: rotate(360deg); } }
  .ring-in { text-align: center; }
  .state { font-weight: 700; font-size: 1.15rem; letter-spacing: .02em; color: var(--muted); }
  .live .state { color: var(--hot); }
  .timer { font: 400 1.35rem "JetBrains Mono", ui-monospace, monospace; margin-top: 2px; }
  .now { min-width: 0; }
  .now .label { color: var(--muted); font-size: .85rem; }
  .now .title { font-size: 1.5rem; font-weight: 700; line-height: 1.25; letter-spacing: -.01em;
    margin: 4px 0 14px; overflow-wrap: anywhere; }
  .pill { display: inline-flex; align-items: center; gap: 8px; padding: 6px 14px; border-radius: 999px;
    background: rgba(255,255,255,.08); border: 1px solid var(--stroke); font-size: .85rem; color: var(--muted); }
  .pill i { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
  .live .pill i { background: var(--ok); box-shadow: 0 0 10px var(--ok); }

  /* form */
  .cols { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
  label.f { display: block; font-size: .85rem; font-weight: 500; color: var(--muted); margin: 0 0 8px 4px; }
  .field { position: relative; margin-bottom: 18px; }
  input[type=text], input[type=url], input[type=password], select {
    width: 100%; font: inherit; color: var(--text); padding: 14px 18px; border-radius: var(--r-md);
    background: rgba(8,4,22,.55); border: 1px solid var(--stroke); transition: border-color .2s, box-shadow .2s;
  }
  select { appearance: none; -webkit-appearance: none; cursor: pointer;
    background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='14' height='8' fill='none' stroke='%23a59dc8' stroke-width='2'><path d='M1 1l6 6 6-6'/></svg>");
    background-repeat: no-repeat; background-position: right 18px center; padding-right: 44px; }
  select option { background: #1a1236; }
  input::placeholder { color: rgba(165,157,200,.55); }
  input:focus-visible, select:focus-visible {
    outline: none; border-color: var(--cyan); box-shadow: 0 0 0 4px rgba(54,226,255,.18); }
  .eye { position: absolute; right: 8px; top: 8px; width: 36px; height: 36px; border-radius: 12px; border: 0;
    background: rgba(255,255,255,.08); color: var(--muted); cursor: pointer; display: grid; place-items: center; }
  .eye:hover { color: var(--text); }
  .has-eye input { padding-right: 56px; }

  .detect { position: absolute; right: 12px; top: 12px; padding: 4px 12px; border-radius: 999px; font-size: .78rem;
    font-weight: 700; display: none; }
  .detect.yt { display: block; background: rgba(255,51,102,.18); color: #ff7a9a; }
  .detect.dr { display: block; background: rgba(54,226,255,.16); color: var(--cyan); }
  .has-detect input { padding-right: 120px; }

  .chips { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; }
  .chip { padding: 8px 16px; border-radius: 999px; font: inherit; font-size: .88rem; font-weight: 500; cursor: pointer;
    color: var(--text); background: rgba(255,255,255,.07); border: 1px solid var(--stroke); transition: all .2s; }
  .chip:hover { background: rgba(255,255,255,.13); }
  .chip[aria-pressed=true] { background: linear-gradient(135deg, var(--hot), var(--amber)); border-color: transparent; color: #1a0a14; font-weight: 700; }

  .toggle { display: flex; align-items: center; justify-content: space-between; gap: 12px; cursor: pointer;
    padding: 12px 16px; border-radius: var(--r-md); background: rgba(8,4,22,.55); border: 1px solid var(--stroke); }
  .toggle b { font-weight: 500; font-size: .95rem; }
  .toggle small { color: var(--muted); display: block; font-size: .8rem; }
  .toggle input { position: absolute; opacity: 0; pointer-events: none; }
  .sw { flex: none; width: 48px; height: 28px; border-radius: 999px; background: rgba(255,255,255,.15); position: relative; transition: background .2s; }
  .sw::after { content: ""; position: absolute; top: 3px; left: 3px; width: 22px; height: 22px; border-radius: 50%; background: #fff; transition: transform .2s; }
  .toggle input:checked + .sw { background: var(--ok); }
  .toggle input:checked + .sw::after { transform: translateX(20px); }
  .toggle:focus-within { box-shadow: 0 0 0 4px rgba(54,226,255,.18); border-color: var(--cyan); }

  /* actions */
  .actions { display: grid; grid-template-columns: 2fr 1fr; gap: 14px; }
  .btn { font: inherit; font-weight: 700; font-size: 1.08rem; padding: 18px 24px; border-radius: 22px; border: 0; cursor: pointer;
    display: flex; align-items: center; justify-content: center; gap: 10px; transition: transform .15s, box-shadow .2s, opacity .2s; }
  .btn:active:not(:disabled) { transform: scale(.98); }
  .btn:disabled { opacity: .4; cursor: not-allowed; }
  .btn:focus-visible { outline: 3px solid var(--cyan); outline-offset: 3px; }
  #run { color: #1b0a12; background: linear-gradient(120deg, var(--hot), var(--amber)); box-shadow: 0 16px 40px -12px rgba(255,51,102,.7); }
  #run:hover:not(:disabled) { box-shadow: 0 20px 50px -10px rgba(255,51,102,.9); }
  #stop { color: var(--text); background: rgba(255,255,255,.08); border: 1px solid var(--stroke); }
  #stop:hover:not(:disabled) { background: rgba(255,51,102,.22); border-color: var(--hot); }

  textarea { width: 100%; font: 12px/1.5 "JetBrains Mono", ui-monospace, monospace; color: var(--text); min-height: 120px;
    padding: 14px 18px; border-radius: var(--r-md); background: rgba(8,4,22,.55); border: 1px solid var(--stroke); resize: vertical; }
  textarea:focus-visible { outline: none; border-color: var(--cyan); box-shadow: 0 0 0 4px rgba(54,226,255,.18); }
  .row { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 12px; flex-wrap: wrap; }
  .mini { font: inherit; font-size: .85rem; font-weight: 500; padding: 8px 16px; border-radius: 999px; cursor: pointer;
    color: var(--text); background: rgba(255,255,255,.08); border: 1px solid var(--stroke); }
  .mini:disabled { opacity: .4; cursor: not-allowed; }
  .mini:hover { background: rgba(255,51,102,.22); border-color: var(--hot); }
  .steps { color: var(--muted); font-size: .88rem; margin: 0 0 14px; padding-left: 20px; }
  .steps li { margin-bottom: 4px; }
  #cookieState { font-size: .85rem; color: var(--muted); }
  #cookieState.on { color: var(--ok); }

  /* console */
  details.card { padding: 0; }
  summary { cursor: pointer; padding: 20px 24px; font-weight: 700; list-style: none; display: flex; justify-content: space-between; align-items: center; }
  summary::-webkit-details-marker { display: none; }
  summary::after { content: "+"; width: 30px; height: 30px; display: grid; place-items: center; border-radius: 50%; background: rgba(255,255,255,.09); color: var(--muted); }
  details[open] summary::after { content: "\2212"; }
  summary:focus-visible { outline: 3px solid var(--cyan); outline-offset: -3px; border-radius: var(--r-xl); }
  pre { margin: 0 14px 14px; padding: 16px 18px; border-radius: var(--r-lg); background: rgba(5,2,16,.8);
    border: 1px solid var(--stroke); color: #c5f3ff; font: 12px/1.65 "JetBrains Mono", ui-monospace, monospace;
    max-height: 260px; overflow: auto; white-space: pre-wrap; word-break: break-all; }

  .note { color: var(--muted); font-size: .82rem; text-align: center; padding: 0 12px; }

  #toast { position: fixed; left: 50%; bottom: 24px; transform: translate(-50%, 140%); z-index: 9;
    max-width: calc(100vw - 32px); padding: 14px 22px; border-radius: 18px; font-weight: 500; background: #241a47;
    border: 1px solid var(--stroke); box-shadow: 0 18px 50px -10px rgba(0,0,0,.8); transition: transform .35s cubic-bezier(.2,.9,.3,1.2); }
  #toast.show { transform: translate(-50%, 0); }
  #toast.err { border-color: var(--hot); } #toast.ok { border-color: var(--ok); }

  @media (max-width: 720px) {
    .hero { grid-template-columns: 1fr; justify-items: center; text-align: center; }
    .cols { grid-template-columns: 1fr; gap: 0; }
    .actions { grid-template-columns: 1fr; }
    .card { border-radius: 28px; padding: 20px; }
  }
  @media (prefers-reduced-motion: reduce) { .live .ring::before { animation: none; } * { transition: none !important; } }
</style>
</head>
<body>
<div class="wrap" id="root">

  <div class="top">
    <div class="logo" aria-hidden="true">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#1b0a12" stroke-width="2.4" stroke-linecap="round"><circle cx="12" cy="12" r="2.2" fill="#1b0a12"/><path d="M7.2 7.2a6.8 6.8 0 0 0 0 9.6M16.8 7.2a6.8 6.8 0 0 1 0 9.6M4.2 4.2a11 11 0 0 0 0 15.6M19.8 4.2a11 11 0 0 1 0 15.6"/></svg>
    </div>
    <div><h1>Stream Deck</h1><span>YouTube or Drive in, anywhere out</span></div>
  </div>

  <section class="card hero" aria-live="polite">
    <div class="ring"><div class="ring-in"><div class="state" id="state">OFFLINE</div><div class="timer" id="timer">00:00:00</div></div></div>
    <div class="now">
      <div class="label" id="nowLabel">Nothing is streaming</div>
      <div class="title" id="nowTitle">Ready when you are</div>
      <span class="pill"><i></i><span id="pillText">Server idle</span></span>
      <div class="label" id="prog" style="margin-top:12px"></div>
    </div>
  </section>

  <form id="form" autocomplete="off" class="wrap" style="gap:18px">
    <div class="cols">
      <section class="card">
        <h2>Source</h2>
        <p class="sub">A YouTube video or live stream, or a Drive file shared with “Anyone with the link”.</p>
        <div class="field has-detect">
          <label class="f" for="video_url">Video link</label>
          <input id="video_url" type="url" placeholder="https://youtube.com/watch?v=…" required>
          <span class="detect" id="detect"></span>
        </div>
        <div class="field" id="qualityField">
          <label class="f" for="quality">YouTube quality</label>
          <select id="quality">
            <option value="1080" selected>Up to 1080p</option>
            <option value="720">Up to 720p</option>
            <option value="480">Up to 480p</option>
            <option value="best">Best available</option>
          </select>
        </div>
        <label class="toggle">
          <span><b>Loop the video</b><small>Restart when it ends (not for live sources)</small></span>
          <input type="checkbox" id="loop"><span class="sw"></span>
        </label>
      </section>

      <section class="card">
        <h2>Destination</h2>
        <p class="sub">Pick a platform, then paste its stream key.</p>
        <div class="chips" id="chips" role="group" aria-label="Platform presets">
          <button type="button" class="chip" data-url="rtmp://a.rtmp.youtube.com/live2">YouTube</button>
          <button type="button" class="chip" data-url="rtmps://live-api-s.facebook.com:443/rtmp/">Facebook</button>
          <button type="button" class="chip" data-url="rtmp://live.twitch.tv/app">Twitch</button>
          <button type="button" class="chip" data-url="rtmps://fa723fc1b171.global-contribute.live-video.net:443/app">Kick</button>
          <button type="button" class="chip" data-url="">Custom</button>
        </div>
        <div class="field">
          <label class="f" for="stream_url">Stream URL</label>
          <input id="stream_url" type="text" placeholder="rtmps://live-api-s.facebook.com:443/rtmp/" required>
        </div>
        <div class="field has-eye" style="margin-bottom:0">
          <label class="f" for="stream_key">Stream key</label>
          <input id="stream_key" type="password" placeholder="Paste your key" required>
          <button type="button" class="eye" id="eye" aria-label="Show stream key">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/></svg>
          </button>
        </div>
        <button type="button" class="mini" id="testBtn" style="margin-top:16px">Send a 30-second test pattern</button>
      </section>
    </div>

    <details class="card" id="cookieBox">
      <summary>YouTube access <span id="cookieState">No cookies saved</span></summary>
      <div style="padding: 0 24px 24px">
        <p class="sub" style="margin-bottom:10px">YouTube often blocks cloud servers. Paste your cookies once and this browser remembers them. Use a spare Google account.</p>
        <ol class="steps">
          <li>On a computer (or Kiwi Browser), open a private window and sign in to YouTube.</li>
          <li>Open youtube.com/robots.txt and export them (Cookie-Editor JSON or a cookies.txt file both work).</li>
          <li>Close the private window, then paste the exported text here.</li>
        </ol>
        <textarea id="cookies" placeholder="# Netscape HTTP Cookie File&#10;.youtube.com&#9;TRUE&#9;/&#9;TRUE&#9;…" spellcheck="false"></textarea>
        <div class="row"><span class="sub" style="margin:0">Sent only to your server when you press Run.</span>
          <button type="button" class="mini" id="clearCookies">Clear cookies</button></div>
      </div>
    </details>

    <div class="actions">
      <button type="submit" class="btn" id="run">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M7 4.5v15a1 1 0 0 0 1.5.86l12-7.5a1 1 0 0 0 0-1.72l-12-7.5A1 1 0 0 0 7 4.5z"/></svg>
        Run
      </button>
      <button type="button" class="btn" id="stop" disabled>
        <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><rect x="5" y="5" width="14" height="14" rx="3"/></svg>
        Stop
      </button>
    </div>
  </form>

  <details class="card" open>
    <summary>FFmpeg console</summary>
    <pre id="log">Nothing running.</pre>
  </details>

  <p class="note">Once it says Live you can close this tab and go offline. The server keeps streaming, and resumes by itself if the link drops, until the video ends or you press Stop.</p>
</div>
<div id="toast" role="status"></div>

<script>
const $ = id => document.getElementById(id);
let uptime = 0, fetchedAt = Date.now(), running = false;

function toast(text, kind) {
  const t = $("toast"); t.textContent = text; t.className = "show " + (kind || "");
  clearTimeout(toast.t); toast.t = setTimeout(() => t.className = "", kind === "err" ? 9000 : 4200);
}
const pad = n => String(n).padStart(2, "0");
function fmt(s) { return pad(Math.floor(s / 3600)) + ":" + pad(Math.floor(s % 3600 / 60)) + ":" + pad(s % 60); }

function tick() { $("timer").textContent = fmt(running ? uptime + Math.floor((Date.now() - fetchedAt) / 1000) : 0); }

async function refresh() {
  try {
    const r = await (await fetch("/status")).json();
    running = r.running; uptime = r.uptime; fetchedAt = Date.now();
    $("root").classList.toggle("live", r.running);
    $("state").textContent = r.running ? "ON AIR" : "OFFLINE";
    $("run").disabled = r.running; $("stop").disabled = !r.running; $("testBtn").disabled = r.running;
    $("nowLabel").textContent = r.running ? "Now streaming" : (r.exit_code !== null && r.title ? "Last stream ended" : "Nothing is streaming");
    $("nowTitle").textContent = r.title || "Ready when you are";
    const src = { youtube: "YouTube", drive: "Google Drive", test: "the test pattern" }[r.source] || "";
    $("pillText").textContent = !r.running ? "Server idle"
      : (r.phase === "reconnecting" ? "Reconnecting to " + src : "Live from " + src)
        + (r.restarts ? ", resumed " + r.restarts + (r.restarts === 1 ? " time" : " times") : "");
    $("state").textContent = r.running ? (r.phase === "reconnecting" ? "RETRYING" : "ON AIR") : "OFFLINE";
    $("prog").textContent = r.running && r.duration ? "Played " + fmt(r.pos) + " of " + fmt(r.duration) : "";
    if (r.logs.length) {
      const el = $("log"), atEnd = el.scrollTop + el.clientHeight >= el.scrollHeight - 24;
      el.textContent = r.logs.join("\n"); if (atEnd) el.scrollTop = el.scrollHeight;
    }
    tick();
  } catch (e) { /* offline is fine: the server keeps streaming */ }
}

// source detection
$("video_url").addEventListener("input", e => {
  const v = e.target.value, d = $("detect");
  const yt = /(youtube\.com|youtu\.be)/i.test(v), dr = /(drive|docs)\.google\.com/i.test(v);
  d.className = "detect " + (yt ? "yt" : dr ? "dr" : "");
  d.textContent = yt ? "YouTube" : dr ? "Drive" : "";
  $("qualityField").style.display = dr ? "none" : "";
});

// ---- remember last values across refreshes
const FIELDS = ["video_url", "stream_url", "stream_key", "quality", "cookies"];
const store = {
  get(k) { try { return localStorage.getItem("sd_" + k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem("sd_" + k, v); } catch (e) {} }
};
const chips = [...document.querySelectorAll(".chip")];

function syncChips() {
  const cur = $("stream_url").value.trim();
  let hit = false;
  chips.forEach(c => { const on = !!c.dataset.url && c.dataset.url === cur; if (on) hit = true; c.setAttribute("aria-pressed", on); });
  const custom = chips.find(c => !c.dataset.url);
  custom.setAttribute("aria-pressed", !hit && cur !== "");
}
function cookieStatus() {
  const on = $("cookies").value.trim().length > 20;
  $("cookieState").textContent = on ? "Cookies saved in this browser" : "No cookies saved";
  $("cookieState").className = on ? "on" : "";
}

FIELDS.forEach(k => ["input", "change"].forEach(ev => $(k).addEventListener(ev, () => {
  store.set(k, $(k).value);
  if (k === "stream_url") syncChips();
  if (k === "cookies") cookieStatus();
})));
$("loop").addEventListener("change", () => store.set("loop", $("loop").checked ? "1" : "0"));

FIELDS.forEach(k => { const v = store.get(k); if (v !== null) $(k).value = v; });
$("loop").checked = store.get("loop") === "1";
$("video_url").dispatchEvent(new Event("input"));
syncChips(); cookieStatus();

// presets
chips.forEach(c => c.addEventListener("click", () => {
  const isPreset = v => chips.some(x => x.dataset.url && x.dataset.url === v);
  if (c.dataset.url) $("stream_url").value = c.dataset.url;
  else if (isPreset($("stream_url").value.trim())) $("stream_url").value = "";
  store.set("stream_url", $("stream_url").value); syncChips();
  (c.dataset.url ? $("stream_key") : $("stream_url")).focus();
}));

$("clearCookies").addEventListener("click", () => { $("cookies").value = ""; store.set("cookies", ""); cookieStatus(); toast("Cookies cleared.", "ok"); });

// show/hide key
$("eye").addEventListener("click", () => {
  const k = $("stream_key"), show = k.type === "password";
  k.type = show ? "text" : "password";
  $("eye").setAttribute("aria-label", show ? "Hide stream key" : "Show stream key");
});

$("form").addEventListener("submit", async e => {
  e.preventDefault();
  $("run").disabled = true; toast("Fetching the video and starting FFmpeg…");
  try {
    const res = await fetch("/start", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ video_url: $("video_url").value, stream_url: $("stream_url").value,
        stream_key: $("stream_key").value, quality: $("quality").value, loop: $("loop").checked,
        cookies: $("cookies").value }) });
    const data = await res.json();
    if (res.ok) { toast("You’re live. You can close this tab.", "ok"); }
    else {
      toast(data.error || "Couldn’t start the stream.", "err");
      if (data.need_cookies) { $("cookieBox").open = true; $("cookieBox").scrollIntoView({ behavior: "smooth", block: "center" }); }
    }
  } catch (err) { toast("Couldn’t reach the server.", "err"); }
  refresh();
});

$("testBtn").addEventListener("click", async () => {
  if (!$("stream_url").value.trim() || !$("stream_key").value.trim()) { toast("Fill in the Stream URL and key first.", "err"); return; }
  toast("Sending a 30-second test pattern…");
  try {
    const res = await fetch("/start", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ test: true, stream_url: $("stream_url").value, stream_key: $("stream_key").value }) });
    const data = await res.json();
    if (res.ok) toast("Test started. Look at the destination’s preview now.", "ok");
    else toast(data.error || "Couldn’t start the test.", "err");
  } catch (err) { toast("Couldn’t reach the server.", "err"); }
  refresh();
});

$("stop").addEventListener("click", async () => {
  await fetch("/stop", { method: "POST" }); toast("Stream stopped.", "ok"); refresh();
});

refresh(); setInterval(refresh, 3000); setInterval(tick, 1000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
