"""Offline character guide coverage, optional throttled catalog refresh and small expansion."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from knowledge.config import DATA_DIR
from llmwiki import WikiStore, WikiError
from llmwiki.collector import Collector
from llmwiki.guide_coverage import coverage, load_catalog, refresh_catalog, select_batch
from llmwiki.updates import UpdateOptions, atomic_json, run_update


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--refresh-catalog", action="store_true", help="按现有限频刷新官方公开角色目录，不抓攻略正文")
    ap.add_argument("--live", action="store_true", help="对本地缺口执行一批采集、编译、审核、发布，产生模型费用")
    ap.add_argument("--batch-size", type=int, default=3)
    ap.add_argument("--limit", type=int, choices=(1,), default=1, help="每角色最新一篇")
    ap.add_argument("--max-pages", type=int, default=6)
    ap.add_argument("--db", default=str(Path(DATA_DIR)/"wiki/wiki.db"))
    ap.add_argument("--knowledge-db", default=str(Path(DATA_DIR)/"knowledge.db"))
    ap.add_argument("--cache-directory", default=str(Path(DATA_DIR)/"source-collection"))
    ap.add_argument("--catalog", help="默认缓存目录下的mcguide-catalog.json")
    ap.add_argument("--output", default=str(Path(DATA_DIR)/"wiki/guide-coverage.json"))
    args = ap.parse_args()
    if not 1 <= args.batch_size <= 5 or not 1 <= args.max_pages <= 100:
        ap.error("每批角色1—5，章节限额1—100")
    path = Path(args.catalog or Path(args.cache_directory)/"mcguide-catalog.json")
    try:
        snapshot = refresh_catalog(Collector(args.cache_directory), path) if args.refresh_catalog else load_catalog(path)
        store = WikiStore(args.db, create=False)
        report = coverage(store, args.knowledge_db, snapshot)
        selected = select_batch(report, args.batch_size)
        report["next_batch"] = selected
        atomic_json(Path(args.output), report)
        print(json.dumps({"mode": "live" if args.live else "coverage", "summary": report["summary"], "next_batch": selected, "report": args.output}, ensure_ascii=False), flush=True)
        if args.live and selected:
            result = run_update(store, directory=store.path.parent/"updates", cache_directory=args.cache_directory,
                                options=UpdateOptions(sites=("mcguide",), roles=tuple(selected), limit=args.limit, max_pages=args.max_pages),
                                progress=lambda event: print(json.dumps(event,ensure_ascii=False),flush=True))
            report = coverage(store, args.knowledge_db, snapshot)
            report.update(next_batch=select_batch(report,args.batch_size), last_update_run_id=result["run_id"])
            atomic_json(Path(args.output),report)
            print(json.dumps({"run_id":result["run_id"],"state":result["state"],"summary":result["summary"],"coverage":report["summary"]},ensure_ascii=False),flush=True)
            return 0 if result["state"] == "completed" else 2
        return int(bool(report["planning_errors"]))
    except (WikiError, OSError, ValueError) as exc:
        print(json.dumps({"state":"failed","error":str(exc)},ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
