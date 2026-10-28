"""End-to-end offline CLI smoke test. Uses only a newly created temporary database."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/wiki"
CLI = ROOT / "scripts/wiki.py"


def main():
    with tempfile.TemporaryDirectory(prefix="verify-wiki-") as folder:
        database = Path(folder) / "wiki.db"
        def call(*args, success=True):
            proc = subprocess.run([sys.executable, "-X", "utf8", str(CLI), "--db", str(database), *map(str, args)],
                                  capture_output=True, encoding="utf-8")
            if success:
                assert proc.returncode == 0, proc.stderr or proc.stdout
                return json.loads(proc.stdout)
            assert proc.returncode == 2, proc.stdout
            return proc
        call("init")
        imported = call("import", FIXTURES / "source.md", "--id", "demo/source", "--version", "demo", "--entity", "demo:resource", "--topic", "demo")
        assert imported["revision"] == 1
        staged = call("stage", FIXTURES / "draft.json")
        assert staged["valid"]
        assert call("search", "演示资源", "--version", "demo") == []
        call("publish", staged["draft_id"], success=False)
        call("review", staged["draft_id"], "--decision", "approved", "--reviewer", "offline-fixture", "--reviewer-type", "agent", "--note", "Verified artificial fixture.")
        call("publish", staged["draft_id"])
        assert len(call("search", "演示资源", "--version", "demo")) == 1
        assert call("plan", "--version", "demo")["uncompiled_sources"] == []
        call("delete-source", "--id", "demo/source", "--version", "demo")
        assert call("search", "演示资源", "--version", "demo") == []
        assert call("plan", "--version", "demo")["compile_order"] == ["demo/resource"]
        call("publish", staged["draft_id"], success=False)
        assert call("source", "--id", "demo/source", "--version", "demo", "--revision", "1")["text"]
        assert call("rebuild-index")["indexed"] == 0
        print("PASS CLI: init -> import -> stage -> publish -> search -> withdraw -> plan -> reject stale -> audit -> rebuild")
    print("PASS isolated temporary database cleaned up")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
