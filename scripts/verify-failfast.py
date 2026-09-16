"""A1 acceptance test.

Before the fix, an EMPTY DEEPSEEK_API_KEY blew up during `import app.main`
with the openai SDK's English OpenAIError ("Missing credentials ... set the
OPENAI_API_KEY environment variable"), so main.py's Chinese fail-fast never
ran -- and the message pointed at the wrong env var entirely.

After the fix: importing is safe, and the refusal happens once, at startup,
with the message we wrote.

Usage: python verify-failfast.py [project-root]
"""
import asyncio
import os
import pathlib
import shutil
import sys

ROOT = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "v1" / "backend"))

SCRATCH = ROOT / ".p0-verify-a1"
shutil.rmtree(SCRATCH, ignore_errors=True)
SCRATCH.mkdir(parents=True)

# Empty string, not unset: python-dotenv skips keys already present in
# os.environ, so this reliably simulates "no usable key configured".
os.environ["DEEPSEEK_API_KEY"] = ""
os.environ["DATABASE_URL"] = "sqlite:///" + (SCRATCH / "app.db").as_posix()


def main() -> int:
    print("1) import app.main with an EMPTY key ... ", end="", flush=True)
    try:
        import app.main as app_main
    except Exception as exc:  # noqa: BLE001
        print("FAIL -> {0}: {1}".format(type(exc).__name__, exc))
        return 1
    print("PASS (import is safe now)")

    print("2) run the lifespan ... ", end="", flush=True)

    async def run():
        async with app_main.lifespan(app_main.app):
            pass

    try:
        asyncio.run(run())
    except RuntimeError as exc:
        msg = str(exc)
        ok = "DEEPSEEK_API_KEY" in msg and "拒绝启动" in msg
        print(("PASS -> " if ok else "FAIL -> ") + msg)
        return 0 if ok else 1
    except Exception as exc:  # noqa: BLE001
        print("FAIL -> {0}: {1}".format(type(exc).__name__, exc))
        return 1
    print("FAIL -> lifespan did NOT refuse to start")
    return 1


try:
    code = main()
finally:
    # Windows 上 SQLite 的文件句柄要等 engine 释放,否则 rmtree 会失败 ——
    # 而 ignore_errors=True 会把这声失败吞掉,于是留下一个没人管的临时目录。
    try:
        from app.database import engine  # noqa: PLC0415

        engine.dispose()
    except Exception:  # noqa: BLE001
        pass
    shutil.rmtree(SCRATCH, ignore_errors=True)
sys.exit(code)
