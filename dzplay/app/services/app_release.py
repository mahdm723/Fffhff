"""The Android app offered on /download: version (static/download/version.json, committed with the
signed APK), size and SHA-256 (computed from the file itself, cached until it changes)."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

DOWNLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "static" / "download"
APK = DOWNLOAD_DIR / "dzplay.apk"
META = DOWNLOAD_DIR / "version.json"
APK_URL = "/download/dzplay.apk"

_lock = threading.Lock()
_cache: dict = {"key": None, "info": None}


def info() -> dict | None:
    if not APK.is_file():
        return None
    st = APK.stat()
    meta_m = META.stat().st_mtime if META.is_file() else 0
    key = (st.st_size, st.st_mtime, meta_m)
    with _lock:
        if _cache["key"] == key:
            return _cache["info"]
        h = hashlib.sha256()
        with APK.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        try:
            meta = json.loads(META.read_text(encoding="utf-8")) if META.is_file() else {}
        except ValueError:
            meta = {}
        data = {
            "version_code": int(meta.get("version_code", 0)),
            "version_name": str(meta.get("version_name", "")),
            "released": meta.get("released"),
            "min_android": meta.get("min_android", "6.0"),
            "notes": [str(n) for n in meta.get("notes", [])][:10],
            "size": st.st_size,
            "size_mb": round(st.st_size / (1024 * 1024), 2),
            "sha256": h.hexdigest(),
            "url": APK_URL,
        }
        _cache.update(key=key, info=data)
        return data
