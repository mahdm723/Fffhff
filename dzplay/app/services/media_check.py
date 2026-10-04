"""Server-side checks and re-encoding of user uploads (V5). Runs in the media worker.

Nothing here touches the database or Telegram: a job names one file in UPLOAD_TMP_DIR and the
limits to apply; the result is either the cleaned output files or a refusal with an Arabic reason.

* The type comes from the file's magic bytes, never from its name or the declared MIME type.
* Every file is re-encoded: images with Pillow (new image, no EXIF / GPS / device / date, rotation
  applied), videos with ffmpeg (H.264/AAC MP4, `-map_metadata -1`, faststart) sized to stay under
  TELEGRAM_STORE_MAX_MB so the Bot API can always download it again.
* Decompression bombs: the pixel count is checked from the header before decoding.
* ffmpeg/ffprobe run with `nice` and an address-space limit (`prlimit`), a timeout, local files only.
* Optional NSFW check (NSFWJS MobileNetV2 weights converted to ONNX, CPU) on the image or on a few
  frames of the video.
"""

from __future__ import annotations

import base64
import io
import logging
import re
import subprocess
import threading
import time
from pathlib import Path

from app.config import Settings

log = logging.getLogger("dzplay.media_check")

JOB_ID = re.compile(r"^[a-f0-9]{32}$")
MODEL_PATH = Path(__file__).resolve().parent.parent / "assets" / "nsfw_mobilenet_v2.onnx"
# NSFWJS classes, in the model's output order
CLASSES = ("drawing", "hentai", "neutral", "porn", "sexy")


class Rejected(Exception):
    """The file is refused; `str(exc)` is shown to the user."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# type detection (magic bytes)
# ---------------------------------------------------------------------------

_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}
_MP4_BRANDS = {b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"M4V ", b"M4VP",
               b"dash", b"3gp4", b"3gp5", b"3gp6", b"3g2a", b"MSNV", b"XAVC", b"f4v ", b"mmp4"}


def sniff(head: bytes) -> tuple[str, str] | None:
    """(kind, type) from the first bytes of a file, e.g. ("image", "jpeg"); None if unknown."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image", "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image", "webp"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "video", "webm"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        compatible = {head[i:i + 4] for i in range(16, min(len(head), 64) - 3, 4)}
        if brand in (b"avif", b"avis"):
            return None
        if brand in _HEIF_BRANDS or (brand in (b"mif1", b"msf1") and compatible & _HEIF_BRANDS):
            return "image", "heic"
        if brand == b"qt  ":
            return "video", "mov"
        if brand in _MP4_BRANDS or compatible & _MP4_BRANDS:
            return "video", "mp4"
        return None
    if head[4:8] in (b"moov", b"mdat", b"wide", b"free", b"skip"):  # old QuickTime files without ftyp
        return "video", "mov"
    return None


# ---------------------------------------------------------------------------
# NSFW model (lazy, shared, one inference at a time)
# ---------------------------------------------------------------------------

_model = None
_model_lock = threading.Lock()


def _session():
    global _model
    if _model is None:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        _model = ort.InferenceSession(str(MODEL_PATH), sess_options=opts, providers=["CPUExecutionProvider"])
    return _model


def nsfw_scores(images: list) -> list[dict[str, float]]:
    """Class probabilities for PIL images (resized to 224x224 like NSFWJS: bilinear, /255)."""
    import numpy as np
    from PIL import Image

    if not images:
        return []
    batch = np.stack([np.asarray(im.convert("RGB").resize((224, 224), Image.BILINEAR), dtype=np.float32) / 255.0
                      for im in images])
    with _model_lock:
        session = _session()
        out = session.run(None, {session.get_inputs()[0].name: batch})[0]
    out = np.asarray(out, dtype=np.float64)
    if not np.allclose(out.sum(axis=1), 1.0, atol=1e-3):  # logits: apply softmax
        out = np.exp(out - out.max(axis=1, keepdims=True))
        out = out / out.sum(axis=1, keepdims=True)
    return [dict(zip(CLASSES, map(float, row))) for row in out]


def nsfw_verdict(scores: list[dict[str, float]], block: float, sexy: float) -> tuple[float, bool]:
    """(highest porn+hentai score, refused?)."""
    worst = max((s["porn"] + s["hentai"] for s in scores), default=0.0)
    worst_sexy = max((s["sexy"] for s in scores), default=0.0)
    return worst, worst >= block or (sexy < 1.0 and worst_sexy >= sexy)


# ---------------------------------------------------------------------------
# subprocesses
# ---------------------------------------------------------------------------


def _run(settings: Settings, args: list[str]) -> subprocess.CompletedProcess:
    from app.services.media import limited

    try:
        return subprocess.run(limited(args, settings.MEDIA_FFMPEG_MEM_MB), capture_output=True, timeout=settings.MEDIA_PROCESS_TIMEOUT,
                              check=False, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise Rejected("timeout", "استغرقت معالجة الملف وقتًا طويلًا. جرّب ملفًا أقصر أو أصغر.") from None
    except FileNotFoundError:
        raise RuntimeError("ffmpeg is not installed") from None


# ---------------------------------------------------------------------------
# images
# ---------------------------------------------------------------------------


def _pillow():
    from PIL import Image

    try:  # HEIC/HEIF photos (iPhone); optional dependency
        import pillow_heif

        pillow_heif.register_heif_opener()
    except ImportError:  # pragma: no cover - listed in requirements
        pass
    return Image


def blur_preview(im) -> str:
    """A tiny, heavily blurred JPEG as a data: URI (shown before a chat picture is opened)."""
    from PIL import ImageFilter

    small = im.convert("RGB").copy()
    small.thumbnail((24, 24))
    small = small.filter(ImageFilter.GaussianBlur(2))
    buf = io.BytesIO()
    small.save(buf, "JPEG", quality=40)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def process_image(settings: Settings, src: Path, out: Path, job: dict) -> dict:
    Image = _pillow()
    from PIL import ImageOps, UnidentifiedImageError

    max_pixels = int(job.get("max_pixels") or settings.IMAGE_MAX_PIXELS)
    Image.MAX_IMAGE_PIXELS = max_pixels
    import warnings

    warnings.simplefilter("error", Image.DecompressionBombWarning)  # between 1x and 2x the limit: refuse too
    bad = Rejected("bad_image", "الصورة غير صالحة أو تالفة.")
    try:
        with Image.open(src) as probe:
            w, h = probe.size
            if w * h > max_pixels:  # checked from the header: no decoding of a bomb
                raise Rejected("too_many_pixels", "أبعاد الصورة كبيرة جدًا.")
            if min(w, h) < int(job.get("min_side") or 1):
                raise Rejected("too_small", "الصورة صغيرة جدًا.")
            if max(w, h) > int(job.get("max_side") or 100000):
                raise Rejected("too_large_dims", "أبعاد الصورة كبيرة جدًا.")
            probe.seek(0)
            im = ImageOps.exif_transpose(probe)  # apply the camera rotation; the metadata itself is dropped
            im = im.convert("RGB")
            im.load()
    except Rejected:
        raise
    except Image.DecompressionBombError:
        raise Rejected("too_many_pixels", "أبعاد الصورة كبيرة جدًا.") from None
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise bad from None
    im.thumbnail((settings.IMAGE_MAX_SIDE, settings.IMAGE_MAX_SIDE))
    clean = Image.new("RGB", im.size)  # a brand-new image: nothing from the original file survives
    clean.paste(im)
    result = {"kind": "image", "width": clean.width, "height": clean.height}
    if job.get("nsfw"):
        started = time.perf_counter()
        scores = nsfw_scores([clean])
        result["nsfw_ms"] = round((time.perf_counter() - started) * 1000, 1)
        worst, refused = nsfw_verdict(scores, float(job["nsfw_block"]), float(job["nsfw_sexy"]))
        result["nsfw"] = round(worst, 4)
        if refused:
            raise Rejected("nsfw", "لا يمكن نشر هذه الصورة: تبدو مخالفة لإرشادات المجتمع.")
    if job.get("blur"):
        result["blur"] = blur_preview(clean)
    clean.save(out, "WEBP", quality=settings.IMAGE_QUALITY, method=4)
    result["size"] = out.stat().st_size
    return result


# ---------------------------------------------------------------------------
# videos
# ---------------------------------------------------------------------------


def _video_bitrate_k(settings: Settings, duration: float, factor: float = 1.0) -> int:
    budget_bits = settings.TELEGRAM_STORE_MAX_MB * 1024 * 1024 * 8 * 0.94 * factor
    total_k = budget_bits / 1000 / max(duration, 1.0)
    return int(min(settings.VIDEO_MAX_BITRATE_K, total_k - settings.VIDEO_AUDIO_BITRATE_K - 24))


def process_video(settings: Settings, src: Path, out_mp4: Path, out_poster: Path, job: dict) -> dict:
    from app.services.media import INPUT_GUARD, MediaError, _box, probe

    try:
        info = probe(settings, src)
    except MediaError as exc:
        raise Rejected("bad_video", str(exc)) from None
    duration = info["duration"]
    if duration <= 0:
        raise Rejected("bad_video", "الفيديو غير صالح.")
    if duration > float(job.get("max_seconds") or 90) + 0.5:
        raise Rejected("too_long", f"مدة الفيديو أطول من الحد ({int(job.get('max_seconds') or 90)} ثانية).")
    if duration < float(job.get("min_seconds") or 0):
        raise Rejected("too_short", "الفيديو قصير جدًا.")
    if info["width"] * info["height"] > 4096 * 4096 or min(info["width"], info["height"]) < 64:
        raise Rejected("bad_dims", "أبعاد الفيديو غير مدعومة.")
    bw, bh = _box(info, settings)
    max_bytes = int(settings.TELEGRAM_STORE_MAX_MB * 1024 * 1024)
    scale = (f"scale=w='min(iw,{bw})':h='min(ih,{bh})':force_original_aspect_ratio=decrease,"
             "scale=trunc(iw/2)*2:trunc(ih/2)*2")
    for factor in (1.0, 0.75, 0.5):
        vk = _video_bitrate_k(settings, duration, factor)
        if vk < 250:
            raise Rejected("too_long", "الفيديو طويل جدًا ليُحفظ بجودة مقبولة.")
        res = _run(settings, [
            settings.FFMPEG_BINARY, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *INPUT_GUARD, "-i", str(src),
            "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "-1", "-map_chapters", "-1", "-sn", "-dn",
            "-vf", scale, "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{vk}k", "-maxrate", f"{vk}k",
            "-bufsize", f"{vk * 2}k", "-profile:v", "high", "-pix_fmt", "yuv420p", "-threads", "2",
            "-c:a", "aac", "-b:a", f"{settings.VIDEO_AUDIO_BITRATE_K}k", "-ac", "2",
            "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
            "-movflags", "+faststart", "-f", "mp4", str(out_mp4)])
        if res.returncode != 0 or not out_mp4.exists() or out_mp4.stat().st_size == 0:
            raise Rejected("bad_video", "تعذّرت معالجة الفيديو.")
        if out_mp4.stat().st_size <= max_bytes:
            break
    else:
        raise Rejected("too_big", "الفيديو كبير جدًا بعد الضغط.")
    seek = "0.5" if duration > 1.0 else "0"
    res = _run(settings, [settings.FFMPEG_BINARY, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-ss", seek,
                          *INPUT_GUARD, "-i", str(out_mp4), "-frames:v", "1", "-vf", f"scale=w='min(iw,{bw})':h=-2",
                          "-q:v", "5", "-map_metadata", "-1", str(out_poster)])
    if res.returncode != 0 or not out_poster.exists():
        raise Rejected("bad_video", "تعذّر إنشاء صورة الغلاف.")
    final = probe(settings, out_mp4)
    result = {"kind": "video", "width": final["width"], "height": final["height"], "duration": final["duration"],
              "size": out_mp4.stat().st_size}
    if job.get("nsfw"):
        started = time.perf_counter()
        frames = _frames(settings, out_mp4, final["duration"], int(job.get("frames") or 4))
        worst, refused = nsfw_verdict(nsfw_scores(frames), float(job["nsfw_block"]), float(job["nsfw_sexy"]))
        result["nsfw"], result["nsfw_ms"] = round(worst, 4), round((time.perf_counter() - started) * 1000, 1)
        if refused:
            raise Rejected("nsfw", "لا يمكن نشر هذا الفيديو: يبدو مخالفًا لإرشادات المجتمع.")
    return result


def _frames(settings: Settings, mp4: Path, duration: float, count: int) -> list:
    from PIL import Image

    from app.services.media import INPUT_GUARD

    frames = []
    pattern = mp4.with_name(mp4.stem + "-f%02d.png")
    rate = max(count / max(duration, 0.1), 0.01)
    res = _run(settings, [settings.FFMPEG_BINARY, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *INPUT_GUARD,
                          "-i", str(mp4), "-vf", f"fps={rate:.5f},scale=224:224", "-frames:v", str(count), str(pattern)])
    for i in range(1, count + 1):
        p = mp4.with_name(mp4.stem + f"-f{i:02d}.png")
        if p.exists():
            with Image.open(p) as im:
                frames.append(im.convert("RGB"))
            p.unlink(missing_ok=True)
    if res.returncode != 0 and not frames:
        raise Rejected("bad_video", "تعذّر فحص الفيديو.")
    return frames


# ---------------------------------------------------------------------------
# one job
# ---------------------------------------------------------------------------

OUTPUTS = {"image": {"img": ".img.webp"}, "video": {"mp4": ".mp4.mp4", "poster": ".poster.jpg"}}


def paths(tmp_dir: Path, job_id: str) -> dict[str, Path]:
    if not JOB_ID.match(job_id):
        raise ValueError("bad job id")
    base = Path(tmp_dir).resolve()
    out = {"in": base / f"{job_id}.in"}
    for kind in OUTPUTS.values():
        for variant, suffix in kind.items():
            out[variant] = base / f"{job_id}{suffix}"
    return out


def cleanup(tmp_dir: Path, job_id: str) -> None:
    try:
        for p in paths(tmp_dir, job_id).values():
            p.unlink(missing_ok=True)
        for p in Path(tmp_dir).glob(f"{job_id}*"):
            p.unlink(missing_ok=True)
    except ValueError:
        pass


def run_job(settings: Settings, tmp_dir: Path, job: dict) -> dict:
    """Process one uploaded file. Returns {"ok": True, ...meta, "outputs": [...]} or {"ok": False, code, error}.
    The input file is always deleted; on refusal no output is left behind."""
    job_id = str(job.get("id") or "")
    p = paths(tmp_dir, job_id)
    started = time.perf_counter()
    try:
        with open(p["in"], "rb") as fh:
            head = fh.read(64)
        found = sniff(head)
        allowed = set(job.get("types") or ())
        if found is None or found[1] not in allowed or found[0] != job.get("kind"):
            raise Rejected("bad_type", "نوع الملف غير مسموح." if found else "الملف ليس صورة أو فيديو صالحًا.")
        if found[0] == "image":
            meta = process_image(settings, p["in"], p["img"], job)
        else:
            meta = process_video(settings, p["in"], p["mp4"], p["poster"], job)
        meta.update(ok=True, type=found[1], outputs=list(OUTPUTS[found[0]]), ms=round((time.perf_counter() - started) * 1000))
        return meta
    except Rejected as exc:
        for variant in ("img", "mp4", "poster"):
            p[variant].unlink(missing_ok=True)
        return {"ok": False, "code": exc.code, "error": str(exc)}
    except FileNotFoundError:
        return {"ok": False, "code": "missing", "error": "انتهت مهلة الرفع، أعد المحاولة."}
    except Exception:  # noqa: BLE001 - a broken file must never crash the worker
        log.exception("media job failed")
        for variant in ("img", "mp4", "poster"):
            p[variant].unlink(missing_ok=True)
        return {"ok": False, "code": "error", "error": "تعذّرت معالجة الملف."}
    finally:
        p["in"].unlink(missing_ok=True)
