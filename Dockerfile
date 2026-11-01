FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp[default] = YouTube link resolver, deno = JS runtime yt-dlp needs for YouTube
RUN pip install --no-cache-dir Flask==3.0.3 gunicorn==22.0.0 "yt-dlp[default]" deno

WORKDIR /app
COPY app.py .

RUN useradd -m appuser
USER appuser

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

# One worker on purpose: the running FFmpeg process is tracked in memory.
CMD gunicorn -w 1 --threads 4 --timeout 120 -b 0.0.0.0:${PORT:-8000} app:app
