"""Compile, independently review and publish bounded dated-document chapters."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from llmwiki import WikiStore
from llmwiki.collector import DOCUMENT_SCOPE, utcnow
from llmwiki.documents import document_parts
from llmwiki.compiler import compile_page, CompileOptions
from llmwiki.reviewer import review_draft


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", action="append")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--db")
    args = ap.parse_args()
    store = WikiStore(args.db)
    directory = store.path.parent / "document-runs" / uuid.uuid4().hex
    directory.mkdir(parents=True)
    report = {"started_at": utcnow(), "mode": "live" if args.live else "plan", "pages": [], "errors": []}
    def save():
        (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.live:
        with sqlite3.connect(store.path) as original, sqlite3.connect(directory / "before.db") as backup:
            original.backup(backup)
    with store.connection() as c:
        sources = [r[0] for r in c.execute("SELECT source_id FROM sources WHERE game_version=? ORDER BY source_id", (DOCUMENT_SCOPE,))]
    for sid in sources:
        if args.source and sid not in args.source:
            continue
        source = store.get_source(sid, game_version=DOCUMENT_SCOPE, include_raw=True)
        if source["deleted"]:
            continue
        try:
            # Give each source its own association; multiple authors are compiled separately.
            meta = source["metadata"]
            entity = "document:" + sid
            if args.live and entity not in meta.get("entities", []):
                meta["entities"] = sorted(set(meta.get("entities", []) + [entity]))
                store.import_source(sid, game_version=DOCUMENT_SCOPE, locale="zh", title=source["title"],
                                    raw=source["raw"], media_type=source["media_type"], origin=source["origin"], metadata=meta)
                source = store.get_source(sid, game_version=DOCUMENT_SCOPE)
            data, parts = document_parts(source)
            for part, entries in enumerate(parts):
                page_id = "document/" + sid + f"/part-{part + 1}"
                item = {"source_id": sid, "page_id": page_id, "part": part, "parts": len(parts),
                        "text_chars": sum(len(e["text"]) for e in entries), "status": "planned"}
                report["pages"].append(item)
                if args.live:
                    print("COMPILE " + page_id, flush=True)
                    compiled = compile_page(store, page_id, game_version=DOCUMENT_SCOPE, kind="reference", entities=[entity], topics=[],
                                            title=data["title"] + (f" · 第{part + 1}/{len(parts)}章" if len(parts) > 1 else ""),
                                            document_part=part, options=CompileOptions(max_tokens=12288, total_output_tokens=24576), runs_dir=directory / "compile")
                    item["compile"] = compiled
                    if compiled.get("published"):
                        item["status"] = "unchanged"
                    elif compiled.get("valid") and compiled.get("draft_id"):
                        print("REVIEW " + page_id, flush=True)
                        reviewed = review_draft(store, compiled["draft_id"], run_dir=directory / "review" / uuid.uuid4().hex)
                        item["review"] = reviewed
                        if reviewed["status"] == "approved":
                            item["publication"] = store.publish(reviewed["draft_id"])
                            item["status"] = "published"
                        else:
                            item["status"] = "needs_attention"
                    else:
                        item["status"] = "compile_failed"
                    print(json.dumps({k: item[k] for k in ("page_id", "status")}, ensure_ascii=False), flush=True)
                save()
        except Exception as exc:
            report["errors"].append({"source_id": sid, "error": str(exc)})
            save()
    report["finished_at"] = utcnow()
    save()
    print(json.dumps({"report": str(directory / "report.json"), "pages": len(report["pages"]), "errors": report["errors"]}, ensure_ascii=False), flush=True)
    return int(bool(report["errors"] or any(p["status"] in ("needs_attention", "compile_failed") for p in report["pages"])))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
