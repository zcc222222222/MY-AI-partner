"""Small, restartable public-source batches; source import only."""
import argparse
import json
import sqlite3
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from llmwiki import WikiStore
from llmwiki.collector import Collector, CollectionError, utcnow
from llmwiki.sources import collect_mcguide, collect_official, collect_bwiki, emit


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--site", choices=["mcguide", "bwiki", "official"], required=True)
    ap.add_argument("--role", action="append", default=[])
    ap.add_argument("--article", type=int, action="append", help="Official article IDs, validated against the public menu")
    ap.add_argument("--limit", type=int, default=1)
    ap.add_argument("--interval", type=float, default=5)
    ap.add_argument("--db")
    ap.add_argument("--directory", default=str(Path(__file__).resolve().parents[1] / "backend/data/source-collection"))
    args = ap.parse_args()
    if not 1 <= args.limit <= 10:
        ap.error("Each batch is limited to 1..10 documents per role/site")
    client = Collector(args.directory, interval=args.interval)
    store = WikiStore(args.db)
    report = {"started_at": utcnow(), "site": args.site, "minimum_interval": client.interval,
              "imported": [], "skipped": [], "errors": [], "publication": "sources_only_pending_compile_and_review"}
    try:
        if args.site == "mcguide":
            collect_mcguide(client, store, args.role or ["1210", "1311", "1408"], args.limit, report)
        elif args.site == "official":
            collect_official(client, store, args.limit, report, args.article)
        else:
            collect_bwiki(client, store, args.limit, report)
    except (CollectionError, OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        report["errors"].append(str(exc))
    finally:
        report["requests"] = client.log
        report["finished_at"] = utcnow()
        target = Path(args.directory) / ("report-" + args.site + "-" + report["started_at"].replace(":", "-") + ".json")
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        emit({"report": str(target), "imported": len(report["imported"]), "errors": report["errors"]})
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
