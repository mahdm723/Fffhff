"""Reels media: fetch from Telegram on demand, prepare for phones, cache on disk.

The server is a relay, not an archive: originals stay in Telegram (we store
only the file_id). Prepared files live in MEDIA_CACHE_DIR with an LRU policy
(MEDIA_CACHE_MAX_GB) and a TTL (MEDIA_CACHE_TTL); an evicted file is simply
fetched and prepared again on the next request.

Video: one light MP4 + a JPEG poster. H.264/AAC sources within limits are only
remuxed with `+faststart` (instant start, seconds of CPU); others are
transcoded to H.264/AAC, max 720x1280 (portrait 720p), CRF + max bitrate.
No HLS: clips are short, faststart + HTTP Range already start playback at
once and allow seeking, and one file per clip keeps the cache simple.

Images: decoded with Pillow (decompression-bomb guard), resized, re-encoded
to WebP — which also drops EXIF/GPS and any other metadata.

Safety: ffmpeg/ffprobe run with an argument list (no shell), only on files
inside our own temp directory, with a timeout. Cache file names are built from
validated internal ids, never from request input (no path traversal).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app import clock
from app.config import Settings
from app.models import MediaCacheEntry, ReelAsset

log = logging.getLogger("dzplay.media")

ASSET_ID = re.compile(r"^[a-f0-9]{32}$")
VARIANTS = {"video": ("mp4", "poster"), "image": ("img",)}
EXTENSIONS = {"mp4": ".mp4", "poster": ".jpg", "img": ".webp"}
CONTENT_TYPES = {"mp4": "video/mp4", "poster": "image/jpeg", "img": "image/webp"}
_TOUCH_EVERY = timedelta(minutes=2)


class MediaError(Exception):
    """Processing failed; the message is safe to show to the admin."""


def _run(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise MediaError("انتهت مهلة معالجة الملف.") from None
    except FileNotFoundError:
        raise MediaError("ffmpeg غير مثبت على الخادم.") from None


def probe(settings: Settings, src: Path) -> dict:
    res = _run([settings.FFPROBE_BINARY, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(src)],
               settings.MEDIA_PROCESS_TIMEOUT)
    if res.returncode != 0:
        raise MediaError("الملف ليس فيديو صالحًا.")
    try:
        data = json.loads(res.stdout or b"{}")
    except ValueError:
        raise MediaError("الملف ليس فيديو صالحًا.") from None
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise MediaError("لا يوجد مسار فيديو في الملف.")
    fmt = data.get("format") or {}

    def num(value, cast=float):
        try:
            return cast(value)
        except (TypeError, ValueError):
            return None

    return {
        "vcodec": video.get("codec_name"), "acodec": audio.get("codec_name") if audio else None,
        "width": num(video.get("width"), int) or 0, "height": num(video.get("height"), int) or 0,
        "pix_fmt": video.get("pix_fmt"), "duration": num(fmt.get("duration")) or 0.0,
        "bitrate": num(fmt.get("bit_rate"), int) or 0,
    }


def _box(info: dict, settings: Settings) -> tuple[int, int]:
    """Max output size keeping orientation: 720x1280 portrait, 1280x720 landscape."""
    short, long_ = settings.VIDEO_MAX_WIDTH, settings.VIDEO_MAX_HEIGHT
    return (short, long_) if info["height"] >= info["width"] else (long_, short)


def prepare_video(settings: Settings, src: Path, out_mp4: Path, out_poster: Path) -> dict:
    info = probe(settings, src)
    bw, bh = _box(info, settings)
    fits = info["width"] <= bw and info["height"] <= bh
    light = info["bitrate"] and info["bitrate"] <= settings.VIDEO_REMUX_MAX_BITRATE_K * 1000
    ff = [settings.FFMPEG_BINARY, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]
    common = ["-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "-1", "-movflags", "+faststart", "-f", "mp4"]
    if (info["vcodec"] == "h264" and info["acodec"] in (None, "aac") and info["pix_fmt"] in (None, "yuv420p")
            and fits and light):
        args = ff + ["-c", "copy"] + common + [str(out_mp4)]
    else:
        scale = (f"scale=w='min(iw,{bw})':h='min(ih,{bh})':force_original_aspect_ratio=decrease,"
                 "scale=trunc(iw/2)*2:trunc(ih/2)*2")
        args = ff + [
            "-vf", scale, "-c:v", "libx264", "-preset", "veryfast", "-crf", str(settings.VIDEO_CRF),
            "-maxrate", f"{settings.VIDEO_MAX_BITRATE_K}k", "-bufsize", f"{settings.VIDEO_MAX_BITRATE_K * 2}k",
            "-profile:v", "high", "-pix_fmt", "yuv420p", "-threads", "2",
            "-c:a", "aac", "-b:a", f"{settings.VIDEO_AUDIO_BITRATE_K}k", "-ac", "2",
        ] + common + [str(out_mp4)]
    res = _run(args, settings.MEDIA_PROCESS_TIMEOUT)
    if res.returncode != 0 or not out_mp4.exists() or out_mp4.stat().st_size == 0:
        raise MediaError("تعذّر تجهيز الفيديو.")
    seek = "0.5" if info["duration"] > 1.0 else "0"
    res = _run([settings.FFMPEG_BINARY, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-ss", seek,
                "-i", str(out_mp4), "-frames:v", "1", "-vf", f"scale=w='min(iw,{bw})':h=-2", "-q:v", "5",
                "-map_metadata", "-1", str(out_poster)], settings.MEDIA_PROCESS_TIMEOUT)
    if res.returncode != 0 or not out_poster.exists():
        raise MediaError("تعذّر إنشاء صورة الغلاف.")
    final = probe(settings, out_mp4)
    return {"width": final["width"], "height": final["height"], "duration": final["duration"]}


def prepare_image(settings: Settings, src: Path, out: Path) -> dict:
    from PIL import Image, ImageOps, UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = settings.IMAGE_MAX_PIXELS
    try:
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)  # apply the camera rotation, then drop all metadata
            im = im.convert("RGB")
            im.thumbnail((settings.IMAGE_MAX_SIDE, settings.IMAGE_MAX_SIDE))
            clean = Image.new("RGB", im.size)
            clean.paste(im)
            clean.save(out, "WEBP", quality=settings.IMAGE_QUALITY, method=4)
            return {"width": clean.width, "height": clean.height}
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError):
        raise MediaError("الصورة غير صالحة أو كبيرة جدًا.") from None


class MediaStore:
    def __init__(self, settings: Settings, database, telegram_factory):
        self.settings = settings
        self.database = database
        self._telegram = telegram_factory
        self.dir = Path(settings.MEDIA_CACHE_DIR).resolve()
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._guard = threading.Lock()

    # -------------------------------------------------------------- paths
    def path_for(self, asset_id: str, variant: str) -> Path:
        if not ASSET_ID.match(asset_id) or variant not in EXTENSIONS:
            raise ValueError("bad media key")
        path = (self.dir / f"{asset_id}.{variant}{EXTENSIONS[variant]}").resolve()
        if path.parent != self.dir:
            raise ValueError("bad media key")
        return path

    def _lock(self, asset_id: str) -> threading.Lock:
        with self._guard:
            return self._locks[asset_id]

    # -------------------------------------------------------------- prepare
    def ensure(self, db: Session, asset: ReelAsset, variant: str) -> Path:
        """Path of a ready file for this asset/variant, fetching + preparing it if needed."""
        if variant not in VARIANTS.get(asset.kind, ()):
            raise ValueError("variant not available for this asset")
        path = self.path_for(asset.id, variant)
        if not path.exists():
            with self._lock(asset.id):
                if not path.exists():
                    self.materialize(db, asset)
        self.touch(db, asset.id, variant)
        return path

    def materialize(self, db: Session, asset: ReelAsset) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".tmp-", dir=self.dir) as tmp_name:
            tmp = Path(tmp_name)
            src = tmp / "source"
            self._telegram().download(asset.tg_file_id, src)
            outputs = {v: tmp / f"out{EXTENSIONS[v]}" for v in VARIANTS[asset.kind]}
            if asset.kind == "video":
                meta = prepare_video(self.settings, src, outputs["mp4"], outputs["poster"])
                asset.duration = meta["duration"]
            else:
                meta = prepare_image(self.settings, src, outputs["img"])
            asset.width, asset.height = meta["width"], meta["height"]
            now = clock.utcnow()
            for variant, out in outputs.items():
                final = self.path_for(asset.id, variant)
                os.replace(out, final)  # atomic: readers never see a half-written file
                key = final.name
                size = final.stat().st_size
                entry = db.get(MediaCacheEntry, key)
                if entry is None:
                    db.add(MediaCacheEntry(key=key, asset_id=asset.id, size=size, created_at=now, last_access=now))
                else:
                    entry.size, entry.last_access = size, now
        asset.ready = True
        db.flush()

    def touch(self, db: Session, asset_id: str, variant: str) -> None:
        key = self.path_for(asset_id, variant).name
        now = clock.utcnow()
        db.execute(update(MediaCacheEntry).where(MediaCacheEntry.key == key, MediaCacheEntry.last_access < now - _TOUCH_EVERY)
                   .values(last_access=now))

    def forget_asset(self, db: Session, asset_id: str) -> None:
        for variant in EXTENSIONS:
            try:
                self.path_for(asset_id, variant).unlink(missing_ok=True)
            except ValueError:
                pass
        db.execute(delete(MediaCacheEntry).where(MediaCacheEntry.asset_id == asset_id))

    # -------------------------------------------------------------- eviction
    def total_size(self, db: Session) -> int:
        return int(db.scalar(select(func.coalesce(func.sum(MediaCacheEntry.size), 0))) or 0)

    def purge(self, db: Session) -> dict[str, int]:
        """TTL first, then least-recently-used until under MEDIA_CACHE_MAX_GB; drop orphans/temp dirs."""
        now = clock.utcnow()
        removed = {"expired": 0, "lru": 0, "orphans": 0}

        def drop(entry: MediaCacheEntry) -> None:
            (self.dir / entry.key).unlink(missing_ok=True)
            db.delete(entry)

        for entry in db.execute(select(MediaCacheEntry).where(
                MediaCacheEntry.last_access < now - timedelta(seconds=self.settings.MEDIA_CACHE_TTL))).scalars().all():
            drop(entry)
            removed["expired"] += 1
        db.flush()
        limit = int(self.settings.MEDIA_CACHE_MAX_GB * 1024 ** 3)
        total = self.total_size(db)
        if total > limit:
            for entry in db.execute(select(MediaCacheEntry).order_by(MediaCacheEntry.last_access)).scalars().all():
                if total <= limit:
                    break
                total -= entry.size
                drop(entry)
                removed["lru"] += 1
        # Assets lose their "ready" flag once any of their files is gone.
        db.flush()
        known = set(db.execute(select(MediaCacheEntry.key)).scalars())
        if self.dir.exists():
            for item in self.dir.iterdir():
                if item.name.startswith(".tmp-"):
                    if item.is_dir() and now.timestamp() - item.stat().st_mtime > 2 * 3600:
                        shutil.rmtree(item, ignore_errors=True)
                    continue
                if item.is_file() and item.name not in known:
                    item.unlink(missing_ok=True)
                    removed["orphans"] += 1
        stale = select(ReelAsset.id).where(ReelAsset.ready.is_(True))
        for asset_id in db.execute(stale).scalars().all():
            if not all((self.dir / self.path_for(asset_id, v).name).exists()
                       for v in VARIANTS.get(db.get(ReelAsset, asset_id).kind, ())):
                db.execute(update(ReelAsset).where(ReelAsset.id == asset_id).values(ready=False))
        return removed
