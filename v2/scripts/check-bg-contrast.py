#!/usr/bin/env python3
"""Measure the worst-case text contrast over the real full-page background.

The page background is three stacked layers (main.css:52-87):
    body::before   static webp
    .bg-video      <video>, object-fit: cover
    body::after    a vertical linear-gradient scrim, alpha
                   --bg-image-scrim (top) .. +0.14 (bottom)

The scrim's comment claims it is "文字对比度的保险" (contrast insurance). This
script checks whether that claim actually holds against the REAL artwork, by
decoding frames from app-background.mp4, compositing the scrim exactly as the
browser does, and computing the WCAG contrast ratio of the text tokens.

Usage:
    python scripts/check-bg-contrast.py <video.mp4> [--scrim 0.6] [--samples 8]
"""

from __future__ import annotations

import argparse
import colorsys  # noqa: F401  (kept for clarity; conversions below are explicit)
import math
import os
import subprocess
import sys
import tempfile


# ---------------------------------------------------------------- colour math

def _srgb_encode(v: float) -> float:
    v = max(0.0, min(1.0, v))
    return 12.92 * v if v <= 0.0031308 else 1.055 * (v ** (1 / 2.4)) - 0.055


def oklch_to_srgb(lightness: float, chroma: float, hue_deg: float) -> tuple[float, float, float]:
    """OKLCH -> sRGB, all channels 0..1. Standard Björn Ottosson conversion."""
    h = math.radians(hue_deg)
    a = chroma * math.cos(h)
    b = chroma * math.sin(h)

    l_ = lightness + 0.3963377774 * a + 0.2158037573 * b
    m_ = lightness - 0.1055613458 * a - 0.0638541728 * b
    s_ = lightness - 0.0894841775 * a - 1.2914855480 * b

    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3

    r = 4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    g = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    bb = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
    return (_srgb_encode(r), _srgb_encode(g), _srgb_encode(bb))


def relative_luminance(rgb: tuple[float, float, float]) -> float:
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg: tuple[float, float, float], bg: tuple[float, float, float]) -> float:
    l1, l2 = relative_luminance(fg), relative_luminance(bg)
    if l1 < l2:
        l1, l2 = l2, l1
    return (l1 + 0.05) / (l2 + 0.05)


# ------------------------------------------------------------- frame sampling

def probe_size(ffmpeg: str, video: str) -> tuple[int, int]:
    out = subprocess.run([ffmpeg, "-hide_banner", "-i", video],
                         capture_output=True).stderr.decode("utf-8", "replace")
    for line in out.splitlines():
        if "Video:" in line and "x" in line:
            for token in line.split(","):
                token = token.strip().split(" ")[0]
                if "x" in token and token.replace("x", "").isdigit():
                    w, h = token.split("x")
                    return int(w), int(h)
    sys.exit("could not determine video size")


def sample_frames(ffmpeg: str, video: str, count: int, width: int, tmp: str) -> list[tuple[int, int, bytes]]:
    """Decode `count` evenly spaced frames as raw RGB24 at `width` px wide."""
    duration = 23.1  # measured; only used to space the samples
    step = max(duration / count, 0.5)
    frames = []
    for i in range(count):
        ts = i * step
        out = os.path.join(tmp, f"f{i}.raw")
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{ts:.2f}",
               "-i", video, "-frames:v", "1", "-vf", f"scale={width}:-2",
               "-f", "rawvideo", "-pix_fmt", "rgb24", out]
        if subprocess.run(cmd, capture_output=True).returncode != 0 or not os.path.exists(out):
            continue
        with open(out, "rb") as fh:
            data = fh.read()
        h = len(data) // (width * 3)
        if h:
            frames.append((width, h, data))
    return frames


# ------------------------------------------------------------------ reporting

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--scrim", type=float, default=0.6)
    ap.add_argument("--samples", type=int, default=8)
    ap.add_argument("--width", type=int, default=160)
    ap.add_argument("--ffmpeg", default=None)
    ap.add_argument("--workdir", default=None,
                    help="existing writable dir for the decoded frames. Needed when "
                         "TEMP is sandboxed: ffmpeg cannot write into a directory "
                         "created by this process there.")
    ap.add_argument("--region", default=None, metavar="X0,Y0,X1,Y1",
                    help="restrict the scan to a sub-rectangle in 0..1 fractions, e.g. "
                         "'0.3,0.86,0.7,1.0' for the band the input footer occupies")
    args = ap.parse_args()

    if not os.path.isfile(args.video):
        sys.exit(f"video not found: {args.video}")

    ffmpeg = args.ffmpeg
    if not ffmpeg:
        from shutil import which
        ffmpeg = which("ffmpeg")
    if not ffmpeg:
        ffmpeg = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe")
    if not os.path.isfile(ffmpeg):
        sys.exit("ffmpeg not found; pass --ffmpeg")

    # Tokens straight out of main.css:1-13
    FG = oklch_to_srgb(0.96, 0.008, 240)
    MUTED = oklch_to_srgb(0.72, 0.02, 250)
    ACCENT = oklch_to_srgb(0.79, 0.13, 205)
    DANGER = oklch_to_srgb(0.66, 0.17, 22)
    SCRIM_TOP = oklch_to_srgb(0.16, 0.022, 255)
    SCRIM_BOTTOM = oklch_to_srgb(0.12, 0.022, 255)

    if args.workdir:
        if not os.path.isdir(args.workdir):
            sys.exit(f"--workdir is not a directory: {args.workdir}")
        frames = sample_frames(ffmpeg, args.video, args.samples, args.width, args.workdir)
    else:
        with tempfile.TemporaryDirectory(prefix="bgcontrast-") as tmp:
            frames = sample_frames(ffmpeg, args.video, args.samples, args.width, tmp)

    if not frames:
        sys.exit("no frames decoded")

    worst_muted = (99.0, None, None)
    worst_accent = (99.0, None, None)
    worst_fg = (99.0, None, None)
    worst_danger = (99.0, None, None)
    muted_samples: list[float] = []

    if args.region:
        parts = [float(p) for p in args.region.split(",")]
        if len(parts) != 4:
            sys.exit("--region must be X0,Y0,X1,Y1")
        rx0, ry0, rx1, ry1 = parts
    else:
        rx0, ry0, rx1, ry1 = 0.0, 0.0, 1.0, 1.0

    # Extra: what scrim value would put --muted at >= 4.5:1 everywhere?
    needed_scrim: list[float] = []

    for fi, (w, h, data) in enumerate(frames):
        for y in range(h):
            fy = y / max(h - 1, 1)
            if not (ry0 <= fy <= ry1):
                continue
            alpha = args.scrim + 0.14 * fy
            scrim = tuple(SCRIM_TOP[c] + (SCRIM_BOTTOM[c] - SCRIM_TOP[c]) * fy for c in range(3))
            base = y * w * 3
            for x in range(0, w, 2):
                fx = x / max(w - 1, 1)
                if not (rx0 <= fx <= rx1):
                    continue
                i = base + x * 3
                px = (data[i] / 255, data[i + 1] / 255, data[i + 2] / 255)
                comp = tuple(alpha * scrim[c] + (1 - alpha) * px[c] for c in range(3))
                cm = contrast_ratio(MUTED, comp)
                muted_samples.append(cm)
                if cm < worst_muted[0]:
                    worst_muted = (cm, fi, px)
                ca = contrast_ratio(ACCENT, comp)
                if ca < worst_accent[0]:
                    worst_accent = (ca, fi, px)
                cd = contrast_ratio(DANGER, comp)
                if cd < worst_danger[0]:
                    worst_danger = (cd, fi, px)
                cf = contrast_ratio(FG, comp)
                if cf < worst_fg[0]:
                    worst_fg = (cf, fi, px)

    if not muted_samples:
        sys.exit("region selected no pixels")

    muted_samples.sort()
    n = len(muted_samples)
    pct = lambda p: muted_samples[min(n - 1, int(n * p))]  # noqa: E731

    print(f"frames={len(frames)}  scrim={args.scrim}  pixels={n}")
    print(f"--fg     min={worst_fg[0]:.2f}:1   (frame {worst_fg[1]}, source px {worst_fg[2]})")
    print(f"--muted  min={worst_muted[0]:.2f}:1   (frame {worst_muted[1]}, source px {worst_muted[2]})")
    print(f"--accent min={worst_accent[0]:.2f}:1   (frame {worst_accent[1]}, source px {worst_accent[2]})")
    print(f"--danger min={worst_danger[0]:.2f}:1   (frame {worst_danger[1]}, source px {worst_danger[2]})")
    print(f"--muted  p1={pct(0.01):.2f}  p5={pct(0.05):.2f}  median={pct(0.5):.2f}")
    below = sum(1 for c in muted_samples if c < 4.5) / n * 100
    print(f"--muted  below 4.5:1 on {below:.1f}% of background pixels")

    # Solve for the scrim value that lifts the worst pixel to 4.5:1.
    for s in [x / 100 for x in range(55, 100)]:
        ok = True
        for (w, h, data) in frames[:3]:
            for y in range(0, h, 3):
                fy = y / max(h - 1, 1)
                alpha = s + 0.14 * fy
                scrim = tuple(SCRIM_TOP[c] + (SCRIM_BOTTOM[c] - SCRIM_TOP[c]) * fy for c in range(3))
                base = y * w * 3
                for x in range(0, w, 4):
                    i = base + x * 3
                    px = (data[i] / 255, data[i + 1] / 255, data[i + 2] / 255)
                    comp = tuple(alpha * scrim[c] + (1 - alpha) * px[c] for c in range(3))
                    if contrast_ratio(MUTED, comp) < 4.5:
                        ok = False
                        break
                if not ok:
                    break
            if not ok:
                break
        if ok:
            needed_scrim.append(s)
    if needed_scrim:
        print(f"smallest scrim giving --muted >= 4.5:1 everywhere: {needed_scrim[0]:.2f}")
    else:
        print("no scrim value up to 0.99 achieves --muted >= 4.5:1 on these frames")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
