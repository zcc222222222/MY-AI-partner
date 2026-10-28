"""D1 acceptance: one command serves both the API and the built SPA.

Run against a project root:

    python scripts/verify-spa-mount.py [project-root]

What it proves
  1. GET /api/v1/health still works AFTER mounting "/"  -> the mount did not
     shadow the API (this is the failure mode the ordering comment warns about)
  2. GET /docs still works                              -> registered before the mount
  3. GET / serves the built index.html                  -> when dist/ exists
  4. GET /assets/<built js> serves the real bundle      -> StaticFiles is live
  5. when dist/ is absent: / is 404 but the API still works -> graceful skip
"""
import os
import pathlib
import shutil
import sys

ROOT = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1]
# 应用根目录名只在这里出现一次(改名时改这一处)
APP_DIR = "v2"
sys.path.insert(0, str(ROOT / APP_DIR / "backend"))

SCRATCH = ROOT / ".d1-verify"
shutil.rmtree(SCRATCH, ignore_errors=True)
SCRATCH.mkdir(parents=True)
os.environ["DEEPSEEK_API_KEY"] = "sk-" + "a" * 32
os.environ["DATABASE_URL"] = "sqlite:///" + (SCRATCH / "app.db").as_posix()

from fastapi.testclient import TestClient  # noqa: E402

import app.main as app_main  # noqa: E402

DIST = ROOT / APP_DIR / "frontend" / "dist"
EXPECT_MOUNTED = DIST.is_dir()
FAILED = []


def check(name, ok, detail=""):
    if not ok:
        FAILED.append(name)
    print(("PASS  " if ok else "FAIL  ") + name + (("   -> " + str(detail)) if detail else ""))


print("target root : " + str(ROOT))
print("dist present: " + str(EXPECT_MOUNTED))

try:
    with TestClient(app_main.app) as client:
        health = client.get("/api/v1/health")
        check("API still reachable after mounting /", health.status_code == 200, health.status_code)

        docs = client.get("/docs")
        check("/docs still reachable", docs.status_code == 200, docs.status_code)

        root = client.get("/")
        if EXPECT_MOUNTED:
            ctype = root.headers.get("content-type", "")
            check("GET / serves the SPA shell", root.status_code == 200, root.status_code)
            check("GET / is HTML", ctype.startswith("text/html"), ctype)
            check("GET / body is the built index.html", 'id="app"' in root.text, root.text[:60])

            bundles = sorted((DIST / "assets").glob("*.js"))
            if bundles:
                asset = client.get("/assets/" + bundles[0].name)
                check("GET /assets/<built js> served by StaticFiles", asset.status_code == 200, asset.status_code)
            else:
                check("a built .js exists under dist/assets", False, "none found")
        else:
            check("GET / is 404 when dist is absent (graceful skip)", root.status_code == 404, root.status_code)
finally:
    # Windows keeps SQLite handles until the engine is disposed; rmtree would
    # fail otherwise and ignore_errors would hide it.
    try:
        from app.database import engine  # noqa: PLC0415

        engine.dispose()
    except Exception:  # noqa: BLE001
        pass
    shutil.rmtree(SCRATCH, ignore_errors=True)

print("")
if FAILED:
    print("FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("D1 CHECKS PASSED")
