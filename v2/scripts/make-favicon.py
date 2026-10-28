#!/usr/bin/env python3
"""Build a multi-size favicon.ico (and an apple-touch-icon.png) from source art.

Why this exists: the crop rectangle is not obvious from the source, so it is
recorded here instead of shipping an unexplained binary.  The Amis source art
is 1467x1479 pixel-art where the head occupies roughly x=157..1317,
y=60..1220 -- the visor band sits at y=654..848 and the mouth at y=1050..1200,
so a crop centred on the eyes alone is mostly forehead and reads badly at 16px.

ffmpeg does the decoding and scaling; this script only assembles the ICO
container, so no image library is needed.

ICO layout (all little-endian):
    ICONDIR       reserved=0, type=1, count          (6 bytes)
    ICONDIRENTRY  x count                            (16 bytes each)
    image data    PNG payloads -- accepted by every current browser and by
                  Windows Vista and later

Requires ffmpeg (same prerequisite as scripts/compress-background-video.ps1).

Usage:
    python scripts/make-favicon.py \
        frontend/public/assets/feixingxuerong.png \
        frontend/public/favicon.ico \
        --crop 1160:1160:157:60 --sizes 16,32,48,64 \
        --apple-touch frontend/public/apple-touch-icon.png
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import struct
import subprocess
import sys
import tempfile

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def find_ffmpeg(explicit: str | None) -> str:
    if explicit:
        if not os.path.isfile(explicit):
            sys.exit(f"--ffmpeg path does not exist: {explicit}")
        return explicit
    found = shutil.which("ffmpeg")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA") or ""
    candidates = [os.path.join(local, r"Microsoft\WinGet\Links\ffmpeg.exe") if local else "",
                  r"C:\ffmpeg\bin\ffmpeg.exe"]
    for cand in candidates:
        if cand and os.path.isfile(cand):
            return cand
    sys.exit("ffmpeg not found -- install it (winget install Gyan.FFmpeg) or pass --ffmpeg <path>.")


def render(ffmpeg: str, source: str, crop: str | None, size: int, out_path: str) -> None:
    filters = [f"crop={crop}"] if crop else []
    filters.append(f"scale={size}:{size}:flags=lanczos")
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
           "-i", source, "-vf", ",".join(filters), "-frames:v", "1", out_path]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        sys.exit(f"ffmpeg failed for {size}px:\n{detail}")


def build_ico(pngs: list[tuple[int, str]], out_path: str) -> int:
    """Assemble PNG payloads into an ICO container; return the byte size."""
    entries, blobs = b"", b""
    offset = 6 + 16 * len(pngs)
    for size, path in pngs:
        with open(path, "rb") as fh:
            data = fh.read()
        dim = 0 if size >= 256 else size  # a directory entry stores 0 for 256px
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    with open(out_path, "wb") as fh:
        fh.write(struct.pack("<HHH", 0, 1, len(pngs)) + entries + blobs)
    return os.path.getsize(out_path)


def verify_ico(path: str, expected: int) -> None:
    """Re-read what was just written, so a malformed container cannot ship."""
    with open(path, "rb") as fh:
        blob = fh.read()
    reserved, kind, count = struct.unpack("<HHH", blob[:6])
    if (reserved, kind) != (0, 1):
        sys.exit(f"verify failed: bad ICO header ({reserved}, {kind})")
    if count != expected:
        sys.exit(f"verify failed: expected {expected} entries, found {count}")
    for i in range(count):
        base = 6 + 16 * i
        dim, _c, _r, _p, _planes, bpp, size, off = struct.unpack("<BBBBHHII", blob[base:base + 16])
        label = dim or 256
        if blob[off:off + 8] != PNG_MAGIC:
            sys.exit(f"verify failed: entry {label}px is not a PNG payload")
        if off + size > len(blob):
            sys.exit(f"verify failed: entry {label}px runs past end of file")
        print(f"  entry {label:>3}px  {size:>6} bytes  bpp={bpp}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Build favicon.ico from source art.")
    ap.add_argument("source", help="source image (any format ffmpeg can decode)")
    ap.add_argument("output", help="destination .ico path")
    ap.add_argument("--crop", default=None, metavar="W:H:X:Y",
                    help="square crop in source pixels, applied before scaling")
    ap.add_argument("--sizes", default="16,32,48,64", help="comma-separated square sizes")
    ap.add_argument("--apple-touch", default=None, metavar="PATH",
                    help="also write an opaque 180x180 PNG for iOS home screens")
    ap.add_argument("--ffmpeg", default=None, help="explicit path to ffmpeg")
    ap.add_argument("--workdir", default=None, metavar="DIR",
                    help="scratch directory for the intermediate PNGs "
                         "(default: a temp dir beside the output file)")
    args = ap.parse_args()

    if not os.path.isfile(args.source):
        sys.exit(f"source image not found: {args.source}")

    if args.crop:
        parts = args.crop.split(":")
        if len(parts) != 4 or not all(p.lstrip("-").isdigit() for p in parts):
            sys.exit(f"--crop must be W:H:X:Y, got {args.crop!r}")
        if parts[0] != parts[1]:
            sys.exit(f"--crop must be square, otherwise the icon is distorted: {args.crop}")

    sizes: list[int] = []
    for token in args.sizes.split(","):
        token = token.strip()
        if not token:
            continue
        if not token.isdigit() or not 1 <= int(token) <= 256:
            sys.exit(f"invalid size {token!r}; must be an integer 1..256")
        sizes.append(int(token))
    if not sizes:
        sys.exit("no sizes given")

    ffmpeg = find_ffmpeg(args.ffmpeg)

    out_dir = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(out_dir, exist_ok=True)
    if args.apple_touch:
        os.makedirs(os.path.dirname(os.path.abspath(args.apple_touch)), exist_ok=True)

    # Intermediates live in a scratch directory.  --workdir pins it to a
    # directory that already exists and is known writable: under a redirected
    # or sandboxed TEMP, a subdirectory created here may not be reachable by
    # the ffmpeg child process, which then fails with EACCES on the muxer.
    if args.workdir:
        if not os.path.isdir(args.workdir):
            sys.exit(f"--workdir is not a directory: {args.workdir}")
        tmp_ctx: object = contextlib.nullcontext(args.workdir)
        owned = False
    else:
        tmp_ctx = tempfile.TemporaryDirectory(prefix="favicon-", ignore_cleanup_errors=True)
        owned = True

    pngs: list[tuple[int, str]] = []
    with tmp_ctx as tmp:
        try:
            for size in sorted(sizes):
                out_png = os.path.join(tmp, f"icon-{size}.png")
                render(ffmpeg, args.source, args.crop, size, out_png)
                pngs.append((size, out_png))

            total = build_ico(pngs, args.output)
            print(f"wrote {args.output}  ({total} bytes, {len(pngs)} sizes)")
            verify_ico(args.output, len(pngs))

            if args.apple_touch:
                render(ffmpeg, args.source, args.crop, 180, args.apple_touch)
                print(f"wrote {args.apple_touch}  ({os.path.getsize(args.apple_touch)} bytes)")
        finally:
            if not owned:  # a TemporaryDirectory cleans up after itself
                for _size, path in pngs:
                    try:
                        os.remove(path)
                    except OSError:
                        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
