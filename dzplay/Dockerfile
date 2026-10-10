FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Security updates of the base image. (V6: no ffmpeg any more — pictures only, decoded with Pillow.)
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip "setuptools>=83" && pip install -r requirements.txt

COPY app ./app
COPY static ./static

# The code stays owned by root (read-only for the app user); only the media cache is writable.
RUN useradd --create-home --uid 10001 dzplay && mkdir -p /data/media-cache && chown -R dzplay /data
USER dzplay

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"

# One worker is enough for the MVP. With REDIS_URL set you can raise --workers.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --no-proxy-headers --no-access-log"]
