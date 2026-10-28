"""One bounded public-knowledge update: collect, compile, review, publish, report."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from knowledge.config import DATA_DIR
from llmwiki import WikiStore, WikiError
from llmwiki.updates import UpdateOptions, UpdateBusy, plan_documents, run_update, error_info


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true", help="实际限速采集并调用模型；省略时仅查看本地待更新计划")
    ap.add_argument("--resume", help="恢复报告中的run_id；成功步骤复用，暂时失败重试，审核驳回保留待处理")
    ap.add_argument("--skip-collection", action="store_true", help="只处理已经导入的待更新来源")
    ap.add_argument("--site", choices=("mcguide", "bwiki", "official"), action="append")
    ap.add_argument("--role", action="append")
    ap.add_argument("--article", type=int, action="append")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--interval", type=float)
    ap.add_argument("--max-pages", type=int)
    ap.add_argument("--db", default=str(Path(DATA_DIR) / "wiki/wiki.db"))
    ap.add_argument("--directory", help="更新报告与断点目录，默认Wiki数据库旁的updates/")
    ap.add_argument("--cache-directory", default=str(Path(DATA_DIR) / "source-collection"))
    args = ap.parse_args()
    if args.resume and not args.live:
        ap.error("恢复任务须传入--live")
    if args.resume and any(v is not None for v in (args.site, args.role, args.article, args.limit, args.interval, args.max_pages)):
        ap.error("恢复时使用原任务配置；需要改变采集范围请创建新任务")
    try:
        store = WikiStore(args.db, create=False)
        if not args.live:
            plan = plan_documents(store)
            print(json.dumps({"mode": "local_plan_no_network_or_model", **plan}, ensure_ascii=False, indent=2))
            return int(bool(plan["errors"]))
        options = UpdateOptions(sites=tuple(args.site or ("mcguide", "bwiki", "official")),
                                roles=tuple(args.role or ("1210", "1311", "1408")), articles=tuple(args.article or ()),
                                limit=args.limit if args.limit is not None else 5,
                                interval=args.interval if args.interval is not None else 5,
                                max_pages=args.max_pages if args.max_pages is not None else 40)
        def progress(event):
            print(json.dumps(event, ensure_ascii=False), flush=True)
        report = run_update(store, directory=args.directory or store.path.parent / "updates", cache_directory=args.cache_directory,
                            options=options, resume=args.resume, collect=not args.skip_collection, progress=progress)
        print(json.dumps({"run_id": report["run_id"], "state": report["state"], "summary": report["summary"],
                          "report": str(Path(args.directory or store.path.parent / "updates") / report["run_id"] / "report.json")}, ensure_ascii=False, indent=2))
        return 0 if report["state"] == "completed" else 2
    except UpdateBusy as exc:
        print(json.dumps({"state": "busy", "error": str(exc)}, ensure_ascii=False))
        return 3
    except KeyboardInterrupt:
        print("更新已中断；按日志中的run_id恢复，已批准发布的结果会保留。", file=sys.stderr)
        return 130
    except (WikiError, OSError, ValueError) as exc:
        print(json.dumps({"state": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    except Exception as exc:
        print(json.dumps({"state": "failed", "error": error_info(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
