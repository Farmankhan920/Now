"""
Stream Deck: restream a YouTube video/live or a Google Drive video to one or many RTMP(S)
destinations at once (YouTube, Facebook, Twitch, Kick, Rooter, custom) using FFmpeg -c copy.
Backend + UI live in this one file.

Env vars:
  APP_PASSWORD  protect the page with a login (strongly recommended)
  APP_USER      login username (default: admin)
  YT_COOKIES    optional: contents of a cookies file (or paste it in Settings)
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

YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
DRIVE_HOSTS = {"drive.google.com", "docs.google.com"}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"

TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
KEEP_RE = re.compile(r"error|fail|refus|timed? ?out|invalid|unable|denied|reset|broken|not found|"
                     r"server|reconnect|warning|exit|^Input #0|^Output #0", re.I)
STALL_SECONDS = 60   # no media progress for this long -> restart ffmpeg
MAX_FAILS = 30       # consecutive failed attempts before giving up
OUT_FAIL_LIMIT = 8   # consecutive "destination refused us" failures before giving up
MAX_DESTINATIONS = 8

lock = threading.Lock()
state = {"jobs": {}, "started": None, "title": "", "source": "", "duration": 0,
         "logs": deque(maxlen=150)}

TEST_SRC = {"test": True, "urls": [], "live": True, "ua": UA,
            "title": "Test pattern (30 seconds)", "duration": 0}


class NeedCookies(ValueError):
    """YouTube blocked the server's IP and wants a logged-in session."""


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


def probe_duration(url):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-user_agent", UA, "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", url],
            capture_output=True, text=True, timeout=25).stdout.strip()
        return int(float(out))
    except Exception:
        return 0


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
                              "Settings, then press Run again.")
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


def resolve_source(job):
    if job["kind"] == "test":
        return dict(TEST_SRC)
    if job["kind"] == "drive":
        return resolve_drive(job["video_url"])
    return resolve_youtube(job["video_url"], job["quality"], job["cookies"])


# ---------------------------------------------------------------- ffmpeg helpers
def is_running():
    return any(j["active"] for j in state["jobs"].values())


def log(msg, job=None):
    state["logs"].append(f"{job['name']} | {msg}" if job else msg)


def hms(sec):
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


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


def build_cmd(src, output, loop, start_at=0.0):
    if src.get("test"):  # small colour bars + tone, to check a destination accepts us
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
            job["progress"] = line
        else:
            if "Error opening output" in line:
                run["out_fail"] = True
            if KEEP_RE.search(line):
                log(line, job)


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
    """Run ffmpeg for one destination and keep it running: if it drops or stalls, resume."""
    fails, out_fails, first, rc = 0, 0, True, None
    try:
        while not job["stop"]:
            if not first:
                job["phase"] = "reconnecting"
                try:
                    src = resolve_source(job)
                except ValueError as e:
                    fails += 1
                    log(f"[could not refresh the video link: {e}]", job)
                    if fails > MAX_FAILS:
                        break
                    nap(job, min(5 + 3 * fails, 30))
                    continue
            first = False
            dur = src.get("duration") or 0
            start_at = resume_point(job, src)
            cmd = build_cmd(src, job["output"], job["loop"], start_at)
            run = {"last_t": -1.0, "beat": time.time()}
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                errors="replace", bufsize=1, start_new_session=True)
            job["proc"] = proc
            job["phase"] = "streaming"
            reader = threading.Thread(target=pump_logs, args=(job, proc, start_at, run), daemon=True)
            reader.start()

            while proc.poll() is None:
                if job["stop"]:
                    break
                if time.time() - run["beat"] > STALL_SECONDS:
                    log(f"[no progress for {STALL_SECONDS}s, restarting ffmpeg]", job)
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
                log("[finished: the video played to the end]", job)
                break

            if run.get("out_fail"):
                out_fails += 1
                if out_fails in (1, 4):
                    log(diagnose(job["output"]), job)
                if out_fails >= OUT_FAIL_LIMIT:
                    log("[this platform keeps refusing the connection, so I stopped it. Get a "
                        "fresh URL and key from the platform, wait until it is ready, then "
                        "press Run again]", job)
                    break
            else:
                out_fails = 0

            fails = 0 if run["last_t"] >= 20 else fails + 1
            if fails > MAX_FAILS:
                log("[giving up after repeated failures]", job)
                break
            wait = min(2 + 3 * fails, 30)
            job["restarts"] += 1
            log(f"[stream dropped (code {rc}); resuming from {hms(job['pos'])} in {wait}s]", job)
            nap(job, wait)
    except Exception as e:  # never let a supervisor die silently
        log(f"[supervisor error: {e}]", job)
    finally:
        job["active"] = False
        job["phase"] = ""
        job["progress"] = ""
        job["exit_code"] = rc
        if job["stop"]:
            log("[stopped]", job)


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
    quality = d.get("quality") or "1080"
    loop = bool(d.get("loop"))
    test = bool(d.get("test"))
    cookies = d.get("cookies") or ""
    raw = d.get("destinations") or []

    if not raw:
        return jsonify(error="Select at least one platform."), 400
    if not (video_url or test):
        return jsonify(error="Paste a YouTube or Google Drive link first."), 400

    dests, seen = [], set()
    for x in raw[:MAX_DESTINATIONS]:
        if not isinstance(x, dict):
            continue
        name = str(x.get("name") or "Platform").strip()[:40]
        url = str(x.get("url") or "").strip().rstrip("/")
        key = str(x.get("key") or "").strip()
        did = str(x.get("id") or secrets.token_hex(4))[:40]
        if did in seen:
            continue
        seen.add(did)
        if not url.startswith(("rtmp://", "rtmps://")) or not urlparse(url).hostname or not key:
            return jsonify(error=f"{name}: needs an rtmp:// or rtmps:// URL and a stream key."), 400
        dests.append({"id": did, "name": name, "key": key,
                      "output": f"{normalize_stream_url(url)}/{key}"})
    if not dests:
        return jsonify(error="Select at least one platform."), 400
    if is_running():
        return jsonify(error="A stream is already running. Stop it first."), 409

    try:
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
        state["jobs"] = {}
        state.update(started=time.time(), title=src["title"], source=kind,
                     duration=src.get("duration") or 0)
        state["logs"].clear()
        for x in dests:
            job = {"id": x["id"], "name": x["name"], "key": x["key"], "output": x["output"],
                   "active": True, "stop": False, "proc": None, "pos": 0.0, "kind": kind,
                   "video_url": video_url, "quality": quality, "cookies": cookies, "loop": loop,
                   "phase": "streaming", "restarts": 0, "progress": "", "exit_code": None}
            state["jobs"][x["id"]] = job
            threading.Thread(target=supervise, args=(job, src), daemon=True).start()
    return jsonify(ok=True, title=src["title"], count=len(dests))


@app.post("/stop")
def stop():
    jobs = list(state["jobs"].values())
    for job in jobs:
        if job["active"]:
            job["stop"] = True
    for job in jobs:
        proc = job.get("proc")
        if job["stop"] and proc and proc.poll() is None:
            proc.terminate()
    for job in jobs:
        proc = job.get("proc")
        if job["stop"] and proc:
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
    return jsonify(ok=True)


@app.get("/status")
def status():
    jobs = list(state["jobs"].values())
    running = any(j["active"] for j in jobs)
    logs = list(state["logs"])
    logs += [f"{j['name']} | {j['progress']}" for j in jobs if j["active"] and j["progress"]]
    return jsonify(
        running=running,
        uptime=int(time.time() - state["started"]) if running and state["started"] else 0,
        title=state["title"], source=state["source"], duration=state["duration"],
        pos=int(max([j["pos"] for j in jobs if j["active"]] or [0])),
        dests=[{"id": j["id"], "name": j["name"], "active": j["active"], "phase": j["phase"],
                "restarts": j["restarts"]} for j in jobs],
        logs=logs)


# ---------------------------------------------------------------- the whole UI
INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
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
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
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
    padding: calc(24px + env(safe-area-inset-top)) 16px calc(130px + env(safe-area-inset-bottom));
  }
  .wrap { max-width: 920px; margin: 0 auto; display: grid; gap: 18px; }
  .view { display: none; gap: 18px; }
  .view.active { display: grid; }
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
  .hero { display: grid; grid-template-columns: auto 1fr; gap: 24px; align-items: center; }
  .ring { width: 150px; height: 150px; position: relative; display: grid; place-items: center; }
  .ring::before {
    content: ""; position: absolute; inset: 0; border-radius: 50%; padding: 6px;
    background: conic-gradient(from 0deg, var(--hot), var(--amber), var(--cyan), var(--hot));
    -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
    -webkit-mask-composite: xor; mask-composite: exclude; opacity: .22; transition: opacity .4s;
  }
  .live .ring::before { opacity: 1; animation: spin 3.2s linear infinite; }
  .live .ring { filter: drop-shadow(0 0 22px rgba(255,51,102,.55)); }
  @keyframes spin { to { transform: rotate(360deg); } }
  .ring-in { text-align: center; }
  .state { font-weight: 700; font-size: 1.02rem; letter-spacing: .02em; color: var(--muted); }
  .live .state { color: var(--hot); }
  .timer { font: 400 1.2rem "JetBrains Mono", ui-monospace, monospace; margin-top: 2px; }
  .now { min-width: 0; }
  .now .label { color: var(--muted); font-size: .85rem; }
  .now .title { font-size: 1.35rem; font-weight: 700; line-height: 1.25; letter-spacing: -.01em;
    margin: 4px 0 12px; overflow-wrap: anywhere; }
  .pill { display: inline-flex; align-items: center; gap: 8px; padding: 6px 14px; border-radius: 999px;
    background: rgba(255,255,255,.08); border: 1px solid var(--stroke); font-size: .85rem; color: var(--muted); }
  .pill i { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); flex: none; }
  .live .pill i { background: var(--ok); box-shadow: 0 0 10px var(--ok); }

  /* fields */
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
  input::placeholder, textarea::placeholder { color: rgba(165,157,200,.55); }
  input:focus-visible, select:focus-visible, textarea:focus-visible {
    outline: none; border-color: var(--cyan); box-shadow: 0 0 0 4px rgba(54,226,255,.18); }
  textarea { width: 100%; font: 12px/1.5 "JetBrains Mono", ui-monospace, monospace; color: var(--text); min-height: 120px;
    padding: 14px 18px; border-radius: var(--r-md); background: rgba(8,4,22,.55); border: 1px solid var(--stroke); resize: vertical; }
  .eye { position: absolute; right: 8px; top: 36px; width: 36px; height: 36px; border-radius: 12px; border: 0;
    background: rgba(255,255,255,.08); color: var(--muted); cursor: pointer; display: grid; place-items: center; }
  .eye:hover { color: var(--text); }
  .has-eye input { padding-right: 56px; }
  .detect { position: absolute; right: 12px; top: 41px; padding: 4px 12px; border-radius: 999px; font-size: .78rem;
    font-weight: 700; display: none; }
  .detect.yt { display: block; background: rgba(255,51,102,.18); color: #ff7a9a; }
  .detect.dr { display: block; background: rgba(54,226,255,.16); color: var(--cyan); }
  .has-detect input { padding-right: 120px; }

  .chips { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
  .chip { padding: 8px 16px; border-radius: 999px; font: inherit; font-size: .88rem; font-weight: 500; cursor: pointer;
    color: var(--text); background: rgba(255,255,255,.07); border: 1px solid var(--stroke); transition: all .2s; }
  .chip:hover { background: rgba(255,255,255,.13); }

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

  .mini { font: inherit; font-size: .85rem; font-weight: 500; padding: 8px 16px; border-radius: 999px; cursor: pointer;
    color: var(--text); background: rgba(255,255,255,.08); border: 1px solid var(--stroke); }
  .mini:hover:not(:disabled) { background: rgba(255,255,255,.15); }
  .mini.danger:hover:not(:disabled) { background: rgba(255,51,102,.22); border-color: var(--hot); }
  .mini:disabled { opacity: .4; cursor: not-allowed; }
  .head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }

  /* platform picker */
  .plist { display: grid; gap: 12px; }
  .plat { display: flex; align-items: center; gap: 14px; width: 100%; text-align: left; font: inherit; color: var(--text);
    padding: 14px 16px; border-radius: var(--r-lg); cursor: pointer; border: 2px solid transparent;
    background: linear-gradient(#1a1337, #1a1337) padding-box, linear-gradient(rgba(255,255,255,.14), rgba(255,255,255,.14)) border-box;
    transition: transform .15s, background .2s; }
  .plat:active:not([aria-disabled=true]) { transform: scale(.985); }
  .plat[aria-checked=true] {
    background: linear-gradient(#241a47, #241a47) padding-box, linear-gradient(120deg, var(--hot), var(--amber)) border-box; }
  .plat[aria-disabled=true] { cursor: default; }
  .plat:focus-visible { outline: 3px solid var(--cyan); outline-offset: 2px; }
  .av { flex: none; width: 46px; height: 46px; border-radius: 16px; display: grid; place-items: center;
    font-weight: 700; font-size: 1.2rem; color: #1b0a12; }
  .pt { flex: 1; min-width: 0; display: block; }
  .pt b { display: block; font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pt small { display: block; color: var(--muted); font-size: .82rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pt small[data-st=live] { color: var(--ok); } .pt small[data-st=retry] { color: var(--amber); } .pt small[data-st=off] { color: var(--muted); }
  .ck { flex: none; width: 32px; height: 32px; border-radius: 50%; border: 2px solid rgba(255,255,255,.28);
    display: grid; place-items: center; transition: all .2s; }
  .ck svg { opacity: 0; transform: scale(.5); transition: all .2s; }
  .plat[aria-checked=true] .ck { background: linear-gradient(135deg, var(--hot), var(--amber)); border-color: transparent; }
  .plat[aria-checked=true] .ck svg { opacity: 1; transform: scale(1); }
  .empty { text-align: center; padding: 26px 12px; color: var(--muted); border: 2px dashed var(--stroke); border-radius: var(--r-lg); }
  .empty p { margin-bottom: 14px; }

  /* settings list */
  .srow { padding: 14px 16px; border-radius: var(--r-lg); background: rgba(8,4,22,.45); border: 1px solid var(--stroke); }
  .srow .top2 { display: flex; align-items: center; gap: 14px; }
  .srow .btns { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
  .steps { color: var(--muted); font-size: .88rem; margin: 0 0 14px; padding-left: 20px; }
  .steps li { margin-bottom: 4px; }
  .row { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 12px; flex-wrap: wrap; }
  #cookieState { font-size: .85rem; color: var(--muted); font-weight: 400; }
  #cookieState.on { color: var(--ok); }

  /* actions */
  .actions { display: grid; grid-template-columns: 2fr 1fr; gap: 14px; }
  .btn { font: inherit; font-weight: 700; font-size: 1.08rem; padding: 18px 24px; border-radius: 22px; border: 0; cursor: pointer;
    display: flex; align-items: center; justify-content: center; gap: 10px; transition: transform .15s, box-shadow .2s, opacity .2s; }
  .btn:active:not(:disabled) { transform: scale(.98); }
  .btn:disabled { opacity: .4; cursor: not-allowed; }
  .btn:focus-visible { outline: 3px solid var(--cyan); outline-offset: 3px; }
  .btn.save { width: 100%; color: #1b0a12; background: linear-gradient(120deg, var(--hot), var(--amber)); box-shadow: 0 16px 40px -12px rgba(255,51,102,.7); }
  #run { color: #1b0a12; background: linear-gradient(120deg, var(--hot), var(--amber)); box-shadow: 0 16px 40px -12px rgba(255,51,102,.7); }
  #run:hover:not(:disabled) { box-shadow: 0 20px 50px -10px rgba(255,51,102,.9); }
  #stop { color: var(--text); background: rgba(255,255,255,.08); border: 1px solid var(--stroke); }
  #stop:hover:not(:disabled) { background: rgba(255,51,102,.22); border-color: var(--hot); }

  /* console */
  details.card { padding: 0; }
  summary { cursor: pointer; padding: 20px 24px; font-weight: 700; list-style: none; display: flex; justify-content: space-between; align-items: center; gap: 10px; }
  summary::-webkit-details-marker { display: none; }
  summary::after { content: "+"; flex: none; width: 30px; height: 30px; display: grid; place-items: center; border-radius: 50%; background: rgba(255,255,255,.09); color: var(--muted); }
  details[open] summary::after { content: "\2212"; }
  summary:focus-visible { outline: 3px solid var(--cyan); outline-offset: -3px; border-radius: var(--r-xl); }
  .pad { padding: 0 24px 24px; }
  pre { margin: 0 14px 14px; padding: 16px 18px; border-radius: var(--r-lg); background: rgba(5,2,16,.8);
    border: 1px solid var(--stroke); color: #c5f3ff; font: 12px/1.65 "JetBrains Mono", ui-monospace, monospace;
    max-height: 260px; overflow: auto; white-space: pre-wrap; word-break: break-all; }
  .note { color: var(--muted); font-size: .82rem; text-align: center; padding: 0 12px; }

  /* bottom navigation */
  .nav { position: fixed; z-index: 8; left: 50%; transform: translateX(-50%);
    bottom: calc(14px + env(safe-area-inset-bottom)); width: min(420px, calc(100vw - 32px));
    display: grid; grid-template-columns: 1fr 1fr; gap: 6px; padding: 6px; border-radius: 30px;
    background: rgba(24,16,52,.78); border: 1px solid var(--stroke);
    backdrop-filter: blur(22px); -webkit-backdrop-filter: blur(22px);
    box-shadow: 0 20px 50px -12px rgba(0,0,0,.85); }
  .nav button { font: inherit; font-weight: 700; font-size: .95rem; color: var(--muted); border: 0; background: transparent;
    border-radius: 24px; padding: 12px 8px; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 8px;
    transition: all .2s; }
  .nav button[aria-current=page] { color: #1b0a12; background: linear-gradient(120deg, var(--hot), var(--amber));
    box-shadow: 0 10px 26px -10px rgba(255,51,102,.8); }
  .nav button:focus-visible { outline: 3px solid var(--cyan); outline-offset: 1px; }

  #toast { position: fixed; left: 50%; bottom: calc(96px + env(safe-area-inset-bottom)); transform: translate(-50%, 200%); z-index: 9;
    max-width: calc(100vw - 32px); padding: 14px 22px; border-radius: 18px; font-weight: 500; background: #241a47;
    border: 1px solid var(--stroke); box-shadow: 0 18px 50px -10px rgba(0,0,0,.8); transition: transform .35s cubic-bezier(.2,.9,.3,1.2); }
  #toast.show { transform: translate(-50%, 0); }
  #toast.err { border-color: var(--hot); } #toast.ok { border-color: var(--ok); }

  @media (max-width: 720px) {
    .hero { grid-template-columns: 1fr; justify-items: center; text-align: center; }
    .actions { grid-template-columns: 1fr; }
    .card { border-radius: 28px; padding: 20px; }
    .pad { padding: 0 20px 20px; }
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
    <div><h1>Stream Deck</h1><span>One video, many platforms</span></div>
  </div>

  <!-- ================= HOME ================= -->
  <main class="view active" id="view-home">
    <section class="card hero" aria-live="polite">
      <div class="ring"><div class="ring-in"><div class="state" id="state">OFFLINE</div><div class="timer" id="timer">00:00:00</div></div></div>
      <div class="now">
        <div class="label" id="nowLabel">Nothing is streaming</div>
        <div class="title" id="nowTitle">Ready when you are</div>
        <span class="pill"><i></i><span id="pillText">Server idle</span></span>
        <div class="label" id="prog" style="margin-top:12px"></div>
      </div>
    </section>

    <section class="card">
      <h2>Source</h2>
      <p class="sub">A YouTube video or live link, or a Drive file shared with “Anyone with the link”.</p>
      <div class="field has-detect">
        <label class="f" for="video_url">Video link</label>
        <input id="video_url" type="url" placeholder="https://youtube.com/watch?v=…">
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
      <div class="head">
        <div><h2>Stream to</h2><p class="sub" style="margin-bottom:14px" id="pickSub">Tap to choose one or more platforms.</p></div>
        <button type="button" class="mini" id="selAll">Select all</button>
      </div>
      <div class="plist" id="homeList" role="group" aria-label="Platforms"></div>
      <div class="empty" id="homeEmpty" hidden>
        <p>No platforms saved yet.</p>
        <button type="button" class="mini" id="goSettings">Add a platform</button>
      </div>
    </section>

    <div class="actions">
      <button type="button" class="btn" id="run">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M7 4.5v15a1 1 0 0 0 1.5.86l12-7.5a1 1 0 0 0 0-1.72l-12-7.5A1 1 0 0 0 7 4.5z"/></svg>
        <span id="runText">Run</span>
      </button>
      <button type="button" class="btn" id="stop" disabled>
        <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><rect x="5" y="5" width="14" height="14" rx="3"/></svg>
        Stop
      </button>
    </div>

    <details class="card" open>
      <summary>FFmpeg console</summary>
      <pre id="log">Nothing running.</pre>
    </details>

    <p class="note">Once it says ON AIR you can close this tab and go offline. The server keeps streaming, resumes by itself if a link drops, and stops when the video ends or you press Stop.</p>
  </main>

  <!-- ================= SETTINGS ================= -->
  <main class="view" id="view-settings">
    <section class="card">
      <h2 id="formTitle">Create RTMP</h2>
      <p class="sub">Save a platform once, then pick it on the Home screen.</p>
      <div class="chips" id="chips" role="group" aria-label="Quick fill">
        <button type="button" class="chip" data-name="YouTube" data-url="rtmp://a.rtmp.youtube.com/live2">YouTube</button>
        <button type="button" class="chip" data-name="Facebook" data-url="rtmps://live-api-s.facebook.com:443/rtmp/">Facebook</button>
        <button type="button" class="chip" data-name="Twitch" data-url="rtmp://live.twitch.tv/app">Twitch</button>
        <button type="button" class="chip" data-name="Kick" data-url="rtmps://fa723fc1b171.global-contribute.live-video.net:443/app">Kick</button>
      </div>
      <div class="field">
        <label class="f" for="p_name">Platform name</label>
        <input id="p_name" type="text" placeholder="e.g. Rooter" maxlength="40" autocomplete="off">
      </div>
      <div class="field">
        <label class="f" for="p_url">Stream URL</label>
        <input id="p_url" type="text" placeholder="rtmps://…" autocomplete="off" autocapitalize="off">
      </div>
      <div class="field has-eye">
        <label class="f" for="p_key">Stream key</label>
        <input id="p_key" type="password" placeholder="Paste the key" autocomplete="off">
        <button type="button" class="eye" id="eye" aria-label="Show stream key">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/></svg>
        </button>
      </div>
      <div class="actions" style="grid-template-columns: 2fr 1fr">
        <button type="button" class="btn save" id="savePlat">Save platform</button>
        <button type="button" class="btn" id="cancelEdit" style="background:rgba(255,255,255,.08);border:1px solid var(--stroke);color:var(--text)" hidden>Cancel</button>
      </div>
    </section>

    <section class="card">
      <h2>Saved platforms</h2>
      <p class="sub">Keys stay in this browser and go to your server only when you press Run or Test.</p>
      <div class="plist" id="setList"></div>
      <div class="empty" id="setEmpty" hidden><p>Nothing saved yet. Create your first RTMP above.</p></div>
    </section>

    <details class="card" id="cookieBox">
      <summary><span>YouTube access</span> <span id="cookieState">No cookies saved</span></summary>
      <div class="pad">
        <p class="sub" style="margin-bottom:10px">YouTube often blocks cloud servers. Paste your cookies once and this browser remembers them. Use a spare Google account.</p>
        <ol class="steps">
          <li>On a computer (or Kiwi Browser), open a private window and sign in to YouTube.</li>
          <li>Open youtube.com/robots.txt and export the cookies (Cookie-Editor JSON or a cookies.txt file both work).</li>
          <li>Close the private window, then paste the exported text here.</li>
        </ol>
        <textarea id="cookies" placeholder="Paste cookies here" spellcheck="false"></textarea>
        <div class="row"><span class="sub" style="margin:0">Sent only to your server when you press Run.</span>
          <button type="button" class="mini danger" id="clearCookies">Clear cookies</button></div>
      </div>
    </details>
  </main>
</div>

<nav class="nav" aria-label="Main">
  <button type="button" id="nav-home" aria-current="page">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 11l9-8 9 8"/><path d="M5 10v10h5v-6h4v6h5V10"/></svg>
    Home
  </button>
  <button type="button" id="nav-settings">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>
    Settings
  </button>
</nav>
<div id="toast" role="status"></div>

<script>
const $ = id => document.getElementById(id);
const CHECK = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#1b0a12" stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
let uptime = 0, fetchedAt = Date.now(), running = false, editingId = null;
let platforms = [], selected = new Set(), lastDests = [];

const store = {
  get(k) { try { return localStorage.getItem("sd_" + k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem("sd_" + k, v); } catch (e) {} }
};
const pad = n => String(n).padStart(2, "0");
const fmt = s => pad(Math.floor(s / 3600)) + ":" + pad(Math.floor(s % 3600 / 60)) + ":" + pad(Math.floor(s % 60));

function toast(text, kind) {
  const t = $("toast"); t.textContent = text; t.className = "show " + (kind || "");
  clearTimeout(toast.t); toast.t = setTimeout(() => t.className = "", kind === "err" ? 9000 : 4200);
}

// ---------- navigation
function show(view, push) {
  ["home", "settings"].forEach(v => {
    $("view-" + v).classList.toggle("active", v === view);
    const b = $("nav-" + v); if (v === view) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
  });
  if (push !== false) history.replaceState(null, "", "#" + view);
  window.scrollTo({ top: 0 });
}
$("nav-home").addEventListener("click", () => show("home"));
$("nav-settings").addEventListener("click", () => show("settings"));
$("goSettings").addEventListener("click", () => show("settings"));
window.addEventListener("hashchange", () => show(location.hash === "#settings" ? "settings" : "home", false));

// ---------- platforms
function loadPlatforms() {
  try { platforms = JSON.parse(store.get("platforms") || "null"); } catch (e) { platforms = null; }
  if (!Array.isArray(platforms)) {
    platforms = [];
    const u = store.get("stream_url"), k = store.get("stream_key");   // migrate the old single destination
    if (u && k) platforms.push({ id: newId(), name: "My platform", url: u, key: k });
    savePlatforms();
  }
  let sel = []; try { sel = JSON.parse(store.get("selected") || "[]"); } catch (e) {}
  selected = new Set(sel.filter(id => platforms.some(p => p.id === id)));
}
const newId = () => Math.random().toString(36).slice(2, 10);
function savePlatforms() { store.set("platforms", JSON.stringify(platforms)); store.set("selected", JSON.stringify([...selected])); }
function hostOf(u) { try { return new URL(u.replace(/^rtmps?:/, "https:")).host; } catch (e) { return u; } }
function avatar(name) {
  let h = 0; for (const c of name) h = (h * 31 + c.charCodeAt(0)) % 360;
  const el = document.createElement("span"); el.className = "av"; el.setAttribute("aria-hidden", "true");
  el.style.background = "linear-gradient(135deg, hsl(" + h + " 85% 64%), hsl(" + ((h + 45) % 360) + " 85% 56%))";
  el.textContent = (name.trim()[0] || "?").toUpperCase(); return el;
}

function renderHome() {
  const list = $("homeList"); list.textContent = "";
  $("homeEmpty").hidden = platforms.length > 0; $("selAll").hidden = platforms.length < 2;
  const st = Object.fromEntries(lastDests.map(d => [d.id, d]));
  platforms.forEach(p => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "plat"; b.dataset.id = p.id;
    b.setAttribute("role", "checkbox"); b.setAttribute("aria-checked", selected.has(p.id));
    b.setAttribute("aria-disabled", running);
    const pt = document.createElement("span"); pt.className = "pt";
    const nm = document.createElement("b"); nm.textContent = p.name;
    const sm = document.createElement("small"); sm.textContent = hostOf(p.url); sm.dataset.id = p.id;
    const d = running && st[p.id];
    if (d) {
      sm.dataset.st = !d.active ? "off" : d.phase === "reconnecting" ? "retry" : "live";
      sm.textContent = !d.active ? "Stopped" : (d.phase === "reconnecting" ? "Reconnecting…" : "Live")
        + (d.restarts ? " (resumed " + d.restarts + "x)" : "");
    }
    pt.append(nm, sm);
    const ck = document.createElement("span"); ck.className = "ck"; ck.innerHTML = CHECK;
    b.append(avatar(p.name), pt, ck);
    b.addEventListener("click", () => {
      if (running) { toast("Stop the stream before changing platforms."); return; }
      selected.has(p.id) ? selected.delete(p.id) : selected.add(p.id);
      savePlatforms(); renderHome();
    });
    list.append(b);
  });
  const n = selected.size;
  $("runText").textContent = n ? "Run on " + n + (n === 1 ? " platform" : " platforms") : "Run";
  $("selAll").textContent = n === platforms.length ? "Clear all" : "Select all";
}

function renderSettings() {
  const list = $("setList"); list.textContent = "";
  $("setEmpty").hidden = platforms.length > 0;
  platforms.forEach(p => {
    const row = document.createElement("div"); row.className = "srow";
    const top = document.createElement("div"); top.className = "top2";
    const pt = document.createElement("span"); pt.className = "pt";
    const nm = document.createElement("b"); nm.textContent = p.name;
    const sm = document.createElement("small"); sm.textContent = hostOf(p.url);
    pt.append(nm, sm); top.append(avatar(p.name), pt);
    const btns = document.createElement("div"); btns.className = "btns";
    const mk = (label, cls, fn) => { const x = document.createElement("button"); x.type = "button"; x.className = "mini " + cls; x.textContent = label; x.addEventListener("click", fn); btns.append(x); return x; };
    const t = mk("Send test", "test", () => sendTest(p)); t.disabled = running;
    mk("Edit", "", () => editPlatform(p));
    mk("Delete", "danger", () => {
      if (!confirm("Delete " + p.name + "?")) return;
      platforms = platforms.filter(x => x.id !== p.id); selected.delete(p.id);
      savePlatforms(); renderAll(); toast("Deleted.", "ok");
    });
    row.append(top, btns); list.append(row);
  });
}
function renderAll() { renderHome(); renderSettings(); }

function resetForm() {
  editingId = null; ["p_name", "p_url", "p_key"].forEach(i => $(i).value = "");
  $("formTitle").textContent = "Create RTMP"; $("savePlat").textContent = "Save platform"; $("cancelEdit").hidden = true;
}
function editPlatform(p) {
  editingId = p.id; $("p_name").value = p.name; $("p_url").value = p.url; $("p_key").value = p.key;
  $("formTitle").textContent = "Edit " + p.name; $("savePlat").textContent = "Update platform"; $("cancelEdit").hidden = false;
  $("formTitle").scrollIntoView({ behavior: "smooth", block: "start" });
}
$("cancelEdit").addEventListener("click", resetForm);

$("savePlat").addEventListener("click", () => {
  const name = $("p_name").value.trim(), url = $("p_url").value.trim(), key = $("p_key").value.trim();
  if (!name) return toast("Give the platform a name.", "err");
  if (!/^rtmps?:\/\/.+/i.test(url)) return toast("Stream URL must start with rtmp:// or rtmps://", "err");
  if (!key) return toast("Paste the stream key.", "err");
  if (editingId) {
    platforms = platforms.map(p => p.id === editingId ? { ...p, name, url, key } : p); toast("Updated.", "ok");
  } else {
    const p = { id: newId(), name, url, key }; platforms.push(p); selected.add(p.id); toast(name + " saved and selected.", "ok");
  }
  savePlatforms(); resetForm(); renderAll();
});

$("chips").addEventListener("click", e => {
  const c = e.target.closest(".chip"); if (!c) return;
  const names = [...document.querySelectorAll(".chip")].map(x => x.dataset.name);
  $("p_url").value = c.dataset.url;
  if (!$("p_name").value.trim() || names.includes($("p_name").value.trim())) $("p_name").value = c.dataset.name;
  $("p_key").focus();
});
$("eye").addEventListener("click", () => {
  const k = $("p_key"), shown = k.type === "password"; k.type = shown ? "text" : "password";
  $("eye").setAttribute("aria-label", shown ? "Hide stream key" : "Show stream key");
});
$("selAll").addEventListener("click", () => {
  if (running) return;
  selected = selected.size === platforms.length ? new Set() : new Set(platforms.map(p => p.id));
  savePlatforms(); renderHome();
});

// ---------- source + cookies (remembered across refreshes)
["video_url", "quality", "cookies"].forEach(k => ["input", "change"].forEach(ev => $(k).addEventListener(ev, () => {
  store.set(k, $(k).value); if (k === "cookies") cookieStatus();
})));
$("loop").addEventListener("change", () => store.set("loop", $("loop").checked ? "1" : "0"));
$("video_url").addEventListener("input", e => {
  const v = e.target.value, d = $("detect");
  const yt = /(youtube\.com|youtu\.be)/i.test(v), dr = /(drive|docs)\.google\.com/i.test(v);
  d.className = "detect " + (yt ? "yt" : dr ? "dr" : ""); d.textContent = yt ? "YouTube" : dr ? "Drive" : "";
  $("qualityField").style.display = dr ? "none" : "";
});
function cookieStatus() {
  const on = $("cookies").value.trim().length > 20;
  $("cookieState").textContent = on ? "Cookies saved in this browser" : "No cookies saved"; $("cookieState").className = on ? "on" : "";
}
$("clearCookies").addEventListener("click", () => { $("cookies").value = ""; store.set("cookies", ""); cookieStatus(); toast("Cookies cleared.", "ok"); });

// ---------- talking to the server
const pickDests = list => list.map(({ id, name, url, key }) => ({ id, name, url, key }));

async function post(path, body) {
  const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  return { ok: res.ok, data: await res.json().catch(() => ({})) };
}
async function sendTest(p) {
  toast("Sending a 30-second test pattern to " + p.name + "…");
  try {
    const r = await post("/start", { test: true, destinations: pickDests([p]) });
    if (r.ok) { toast("Test started. Check " + p.name + "’s preview now.", "ok"); show("home"); }
    else toast(r.data.error || "Couldn’t start the test.", "err");
  } catch (e) { toast("Couldn’t reach the server.", "err"); }
  refresh();
}
$("run").addEventListener("click", async () => {
  if (!$("video_url").value.trim()) { toast("Paste a YouTube or Drive link first.", "err"); $("video_url").focus(); return; }
  if (!selected.size) { toast(platforms.length ? "Tap at least one platform to stream to." : "Add a platform in Settings first.", "err"); return; }
  $("run").disabled = true; toast("Fetching the video and starting FFmpeg…");
  try {
    const r = await post("/start", { video_url: $("video_url").value, quality: $("quality").value, loop: $("loop").checked,
      cookies: $("cookies").value, destinations: pickDests(platforms.filter(p => selected.has(p.id))) });
    if (r.ok) toast("You’re live on " + r.data.count + (r.data.count === 1 ? " platform" : " platforms") + ". You can close this tab.", "ok");
    else {
      toast(r.data.error || "Couldn’t start the stream.", "err");
      if (r.data.need_cookies) { show("settings"); $("cookieBox").open = true; $("cookieBox").scrollIntoView({ behavior: "smooth", block: "center" }); }
    }
  } catch (err) { toast("Couldn’t reach the server.", "err"); }
  refresh();
});
$("stop").addEventListener("click", async () => { await post("/stop"); toast("Stopped.", "ok"); refresh(); });

function tick() { $("timer").textContent = fmt(running ? uptime + Math.floor((Date.now() - fetchedAt) / 1000) : 0); }
async function refresh() {
  try {
    const r = await (await fetch("/status")).json();
    const was = running; running = r.running; uptime = r.uptime; fetchedAt = Date.now(); lastDests = r.dests || [];
    const live = lastDests.filter(d => d.active && d.phase === "streaming").length, total = lastDests.filter(d => d.active).length;
    $("root").classList.toggle("live", r.running);
    $("state").textContent = !r.running ? "OFFLINE" : live ? "ON AIR" : "RETRYING";
    $("run").disabled = r.running; $("stop").disabled = !r.running;
    $("nowLabel").textContent = r.running ? "Now streaming" : (lastDests.length ? "Last stream ended" : "Nothing is streaming");
    $("nowTitle").textContent = r.title || "Ready when you are";
    $("pillText").textContent = !r.running ? "Server idle" : (live ? "Live on " : "Reconnecting ") + (live || total) + " of " + total + (total === 1 ? " platform" : " platforms");
    $("prog").textContent = r.running && r.duration ? "Played " + fmt(r.pos) + " of " + fmt(r.duration) : "";
    if (r.logs.length) {
      const el = $("log"), atEnd = el.scrollTop + el.clientHeight >= el.scrollHeight - 24;
      el.textContent = r.logs.join("\n"); if (atEnd) el.scrollTop = el.scrollHeight;
    }
    const sig = running ? JSON.stringify(lastDests) : "idle";   // re-draw only when something changed,
    if (sig !== refresh.sig) { refresh.sig = sig; renderHome(); }   // so taps are never lost to a refresh
    if (was !== running) renderSettings();
    tick();
  } catch (e) { /* offline is fine: the server keeps streaming */ }
}

// ---------- boot
["video_url", "quality", "cookies"].forEach(k => { const v = store.get(k); if (v !== null) $(k).value = v; });
$("loop").checked = store.get("loop") === "1";
$("video_url").dispatchEvent(new Event("input")); cookieStatus();
loadPlatforms(); renderAll();
show(location.hash === "#settings" ? "settings" : "home", false);
refresh(); setInterval(refresh, 3000); setInterval(tick, 1000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
