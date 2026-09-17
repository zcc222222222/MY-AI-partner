#!/usr/bin/env python3
"""Verify the cache policy the backend puts on the built SPA.

Why this exists: the default StaticFiles sends ETag / Last-Modified but **no
Cache-Control**, so browsers fall back to heuristic freshness (roughly 10% of
the file's age) and skip even the conditional request.  The visible symptom is
"rebuilt the frontend but the browser still serves the old shell" -- which also
hides anything only present in the new shell, a favicon <link> for instance.

The rule under test:
    assets/<name>-<8-char hash>.{js,css}  ->  public, max-age=31536000, immutable
    everything else                       ->  no-cache   (revalidate, 304 on hit)

Usage:
    python v1/scripts/verify-spa-cache.py
    python v1/scripts/verify-spa-cache.py --dist <some other dist dir>
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.normpath(os.path.join(HERE, "..", "backend"))
DEFAULT_DIST = os.path.normpath(os.path.join(BACKEND, "..", "frontend", "dist"))
sys.path.insert(0, BACKEND)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import SpaStaticFiles  # noqa: E402

IMMUTABLE = "public, max-age=31536000, immutable"
NO_CACHE = "no-cache"

failures: list[str] = []
checks = 0


def check(label: str, actual, expected) -> None:
    global checks
    checks += 1
    if actual == expected:
        print(f"  PASS  {label}  -> {actual}")
    else:
        print(f"  FAIL  {label}  -> got {actual!r}, want {expected!r}")
        failures.append(label)


def hashed_assets(dist: str) -> tuple[str, str] | None:
    """Return (js, css) entry names from a real build, or None if not found."""
    assets = os.path.join(dist, "assets")
    if not os.path.isdir(assets):
        return None
    names = os.listdir(assets)
    js = next((n for n in names if n.endswith(".js") and "-" in n), None)
    css = next((n for n in names if n.endswith(".css") and "-" in n), None)
    return (js, css) if js and css else None


def run(dist: str) -> None:
    app = FastAPI()
    app.mount("/", SpaStaticFiles(directory=dist, html=True), name="spa")
    client = TestClient(app)

    print(f"dist = {dist}")

    print("\n[1] the SPA shell and the icon must revalidate")
    r = client.get("/")
    check("GET /  status", r.status_code, 200)
    check("GET /  cache-control", r.headers.get("cache-control"), NO_CACHE)
    etag = r.headers.get("etag")

    r = client.get("/favicon.ico")
    check("GET /favicon.ico  status", r.status_code, 200)
    check("GET /favicon.ico  cache-control", r.headers.get("cache-control"), NO_CACHE)

    print("\n[2] hashed build output is immutable")
    built = hashed_assets(dist)
    if built is None:
        check("a hashed js+css pair exists in assets/", built, "not None")
    else:
        for name in built:
            r = client.get(f"/assets/{name}")
            check(f"GET /assets/{name}  status", r.status_code, 200)
            check(f"GET /assets/{name}  cache-control", r.headers.get("cache-control"), IMMUTABLE)

    print("\n[3] plain-named public assets must NOT be immutable")
    for name in ("assets/app-background.webp", "assets/amis-portrait.jpg"):
        r = client.get("/" + name)
        if r.status_code == 200:
            check(f"GET /{name}  cache-control", r.headers.get("cache-control"), NO_CACHE)
        else:
            check(f"GET /{name}  status", r.status_code, 200)

    print("\n[4] media range requests still work and keep the header")
    r = client.get("/assets/app-background.mp4", headers={"Range": "bytes=0-99"})
    check("range status", r.status_code, 206)
    # The total size varies between builds, so only the requested window is asserted.
    check("range content-range prefix",
          r.headers.get("content-range", "").startswith("bytes 0-99/"), True)
    check("range cache-control", r.headers.get("cache-control"), NO_CACHE)

    print("\n[5] a conditional request is answered 304 and still carries the policy")
    if etag:
        r = client.get("/", headers={"If-None-Match": etag})
        check("revalidate status", r.status_code, 304)
        check("revalidate cache-control", r.headers.get("cache-control", NO_CACHE), NO_CACHE)
    else:
        check("etag present", etag, "<etag>")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dist", default=DEFAULT_DIST,
                    help=f"the built frontend directory (default: {DEFAULT_DIST})")
    args = ap.parse_args()

    if not os.path.isdir(args.dist):
        sys.exit(f"not a directory: {args.dist}\nRun `npm run build` in v1/frontend first.")

    run(os.path.abspath(args.dist))

    print()
    if failures:
        print(f"{checks - len(failures)}/{checks} checks passed; FAILURES:")
        for name in failures:
            print(f"  - {name}")
        return 1
    print(f"ALL {checks} CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
