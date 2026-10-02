"""Test-only stand-in for `ffprobe -print_format json -show_streams -show_format FILE`.

The sandbox has a real ffmpeg (imageio-ffmpeg) but no ffprobe binary; this
prints the same JSON fields app.services.media.probe reads, using PyAV (which
bundles FFmpeg's libraries). Production uses the real ffprobe from Debian.
"""

import json
import sys

import av


def main() -> int:
    path = sys.argv[-1]
    try:
        container = av.open(path)
    except Exception:  # noqa: BLE001 - mimic ffprobe's failure exit code
        print("Invalid data found when processing input", file=sys.stderr)
        return 1
    streams = []
    for s in container.streams:
        item = {"codec_type": s.type, "codec_name": s.codec_context.name}
        if s.type == "video":
            item.update(width=s.codec_context.width, height=s.codec_context.height,
                        pix_fmt=s.codec_context.pix_fmt)
        streams.append(item)
    duration = (container.duration or 0) / 1_000_000
    print(json.dumps({"streams": streams, "format": {"duration": str(duration), "bit_rate": str(container.bit_rate or 0)}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
