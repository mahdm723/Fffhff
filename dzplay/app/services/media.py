"""User pictures: fetch from Telegram on demand, cache on disk.

The server is a relay, not an archive: files stay in Telegram (we store only the file_id).
Prepared copies live in MEDIA_CACHE_DIR with an LRU policy (MEDIA_CACHE_MAX_GB) and a TTL
(MEDIA_CACHE_TTL); an evicted file is simply fetched and prepared again on the next request.

Images are decoded with Pillow (decompression-bomb guard), resized and re-encoded to WebP —
which also drops EXIF/GPS and any other metadata. Cache file names are built from validated
internal ids, never from request input (no path traversal).

(V6 removed videos together with Reels: no ffmpeg on the request path any more.)
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import threading
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app import clock
from app.config import Settings
from app.models import MediaCacheEntry, MediaItem

log = logging.getLogger("dzplay.media")

ASSET_ID = re.compile(r"^[a-f0-9]{32}$")
VARIANTS = {"image": ("img",)}
EXTENSIONS = {"img": ".webp"}
CONTENT_TYPES = {"img": "image/webp"}
_TOUCH_EVERY = timedelta(minutes=2)


class MediaError(Exception):
    """Processing failed; the message is safe to show to the admin."""


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
    def ensure(self, db: Session, asset: MediaItem, variant: str) -> Path:
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

    def materialize(self, db: Session, asset: MediaItem) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".tmp-", dir=self.dir) as tmp_name:
            tmp = Path(tmp_name)
            src = tmp / "source"
            self._telegram().download(asset.tg_file_id, src)
            outputs = {v: tmp / f"out{EXTENSIONS[v]}" for v in VARIANTS[asset.kind]}
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

    def adopt(self, db: Session, asset, files: dict[str, Path]) -> None:
        """V5: files the media worker already prepared go straight into the cache (no Telegram round trip)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        now = clock.utcnow()
        for variant, src in files.items():
            final = self.path_for(asset.id, variant)
            tmp = final.with_name(".tmp-" + final.name)
            shutil.copyfile(src, tmp)  # the upload area may be another filesystem (tmpfs)
            os.replace(tmp, final)
            entry = db.get(MediaCacheEntry, final.name)
            size = final.stat().st_size
            if entry is None:
                db.add(MediaCacheEntry(key=final.name, asset_id=asset.id, size=size, created_at=now, last_access=now))
            else:
                entry.size, entry.last_access = size, now
        asset.ready = all(self.path_for(asset.id, v).exists() for v in VARIANTS.get(asset.kind, ()))
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
        for asset_id, kind in db.execute(select(MediaItem.id, MediaItem.kind).where(MediaItem.ready.is_(True))).all():
            if not all((self.dir / self.path_for(asset_id, v).name).exists() for v in VARIANTS.get(kind, ())):
                db.execute(update(MediaItem).where(MediaItem.id == asset_id).values(ready=False))
        return removed
