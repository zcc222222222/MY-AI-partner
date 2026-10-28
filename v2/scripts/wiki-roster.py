"""Roster coverage and bounded LLM compilation; plan is offline, compile calls the model."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from llmwiki import WikiStore, WikiError
from llmwiki.roster import plan_roster, compile_roster


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "compile"])
    parser.add_argument("--version", required=True)
    parser.add_argument("--locale", default="zh")
    parser.add_argument("--db")
    parser.add_argument("--knowledge-db")
    parser.add_argument("--character-id", action="append")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--output")
    args = parser.parse_args()
    try:
        store = WikiStore(args.db)
        options = dict(game_version=args.version, locale=args.locale, knowledge_path=args.knowledge_db)
        if args.command == "plan":
            result = plan_roster(store, **options)
            if args.output:
                path = Path(args.output)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            path = args.output or store.path.parent / "batches" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8] + ".json")
            result = compile_roster(store, **options, character_ids=args.character_id, limit=args.limit, report_path=path)
            result = {"status": result["status"], "report_path": str(path), "results": result["results"]}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("status") == "partial_failure" else 0
    except (WikiError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
