"""Local administration CLI for source revisions, compile plans and safe publication."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from llmwiki import WikiError, WikiStore
from llmwiki.ingest import import_file, sync_nanoka
from llmwiki.payload import KINDS


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", help="独立Wiki数据库路径，默认backend/data/wiki/wiki.db")
    ap.add_argument("--output", help="同时将JSON结果保存为UTF-8文件，避免终端重定向改变编码")
    commands = ap.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    def scoped(name):
        sub = commands.add_parser(name)
        sub.add_argument("--version", required=True)
        sub.add_argument("--locale", default="zh")
        return sub
    def tagged(sub):
        sub.add_argument("--entity", action="append", dest="entities")
        sub.add_argument("--topic", action="append", dest="topics")
    sub = scoped("import")
    sub.add_argument("path")
    sub.add_argument("--id", required=True)
    sub.add_argument("--title")
    sub.add_argument("--origin")
    tagged(sub)
    sub = scoped("import-guide")
    sub.add_argument("manifest")
    sub.add_argument("snapshot")
    sub.add_argument("--id", required=True)
    sub.add_argument("--knowledge-db")
    sub = scoped("sync-nanoka")
    sub.add_argument("--kind", required=True)
    sub.add_argument("--id", required=True)
    sub.add_argument("--knowledge-db")
    for command in ("delete-source", "source", "page"):
        sub = scoped(command)
        sub.add_argument("--id", required=True)
        if command != "delete-source":
            sub.add_argument("--revision", type=int)
        if command == "page":
            sub.add_argument("--allow-stale", action="store_true")
    scoped("plan")
    scoped("refresh")
    sub = scoped("compact-page")
    sub.add_argument("--id", required=True)
    sub.add_argument("--kind", choices=sorted(KINDS))
    sub = scoped("prepare")
    sub.add_argument("--id", required=True)
    sub.add_argument("--title")
    sub.add_argument("--kind", choices=sorted(KINDS))
    tagged(sub)
    sub = scoped("compile")
    sub.add_argument("--id", required=True)
    sub.add_argument("--title")
    sub.add_argument("--kind", choices=sorted(KINDS))
    sub.add_argument("--force", action="store_true", help="忽略相同输入缓存重新编译")
    tagged(sub)
    sub.add_argument("--publish", action="store_true", help="兼容旧参数；新草稿须审核后单独publish")
    sub.add_argument("--max-tokens", type=int, default=8192)
    sub.add_argument("--total-output-tokens", type=int, default=16384)
    sub.add_argument("--max-input-chars", type=int, default=64000)
    sub.add_argument("--attempts", type=int, default=2)
    sub.add_argument("--runs-dir")
    sub = commands.add_parser("stage")
    sub.add_argument("path", help="编译器生成的JSON草稿")
    for command in ("lint", "publish"):
        commands.add_parser(command).add_argument("draft_id")
    sub = commands.add_parser("review")
    sub.add_argument("draft_id")
    sub.add_argument("--decision", required=True, choices=["approved", "rejected"])
    sub.add_argument("--reviewer", required=True)
    sub.add_argument("--reviewer-type", default="human", choices=["human", "agent"])
    sub.add_argument("--note", required=True)
    sub = commands.add_parser("semantic-review", help="单独模型上下文逐条审核；仍需显式publish")
    sub.add_argument("draft_id")
    sub.add_argument("--runs-dir")
    sub.add_argument("--max-passes", type=int, choices=[1, 2], default=2)
    sub = scoped("search")
    sub.add_argument("query")
    sub.add_argument("--limit", type=int, default=5)
    commands.add_parser("rebuild-index")
    for command in ("index-jobs", "events"):
        sub = commands.add_parser(command)
        sub.add_argument("--after", type=int, default=0)
        sub.add_argument("--limit", type=int, default=100)
    commands.add_parser("index-target").add_argument("job_id", type=int)
    args = ap.parse_args(argv)
    try:
        store = WikiStore(args.db)
        scope = {"game_version": args.version, "locale": args.locale} if hasattr(args, "version") else {}
        cmd = args.command
        if cmd == "init":
            result = {"database": str(store.path), "schema_version": 2}
        elif cmd == "import":
            result = import_file(store, args.path, source_id=args.id, title=args.title, origin=args.origin,
                                 entities=args.entities, topics=args.topics, **scope)
        elif cmd == "sync-nanoka":
            result = sync_nanoka(store, kind=args.kind, doc_id=args.id, knowledge_path=args.knowledge_db, **scope)
        elif cmd == "import-guide":
            from llmwiki.guides import import_guide
            result = import_guide(store, args.manifest, args.snapshot, source_id=args.id,
                                  knowledge_path=args.knowledge_db, **scope)
        elif cmd == "delete-source":
            result = store.delete_source(args.id, **scope)
        elif cmd == "source":
            result = store.get_source(args.id, revision=args.revision, **scope)
        elif cmd == "page":
            result = store.get_page(args.id, revision=args.revision, allow_stale=args.allow_stale, **scope)
        elif cmd == "plan":
            result = store.plan(**scope)
        elif cmd == "refresh":
            result = store.refresh(**scope)
        elif cmd == "compact-page":
            result = store.compact_page(args.id, kind=args.kind, **scope)
        elif cmd == "prepare":
            result = store.prepare(args.id, title=args.title, entities=args.entities, topics=args.topics, kind=args.kind, **scope)
        elif cmd == "compile":
            from llmwiki.compiler import CompileOptions, compile_page
            options = CompileOptions(max_tokens=args.max_tokens, total_output_tokens=args.total_output_tokens,
                                     max_input_chars=args.max_input_chars, attempts=args.attempts)
            result = compile_page(store, args.id, title=args.title, entities=args.entities, topics=args.topics,
                                  options=options, publish=args.publish, runs_dir=args.runs_dir, kind=args.kind, force=args.force,
                                  progress=lambda data: print("wiki.compile " + json.dumps(data), file=sys.stderr), **scope)
        elif cmd == "stage":
            result = store.stage_page(json.loads(Path(args.path).read_text(encoding="utf-8-sig")))
        elif cmd == "lint":
            result = store.lint_draft(args.draft_id)
        elif cmd == "publish":
            result = store.publish(args.draft_id)
        elif cmd == "review":
            result = store.review(args.draft_id, decision=args.decision, reviewer=args.reviewer, note=args.note, reviewer_type=args.reviewer_type)
        elif cmd == "semantic-review":
            from llmwiki.reviewer import review_draft
            import uuid
            directory = Path(args.runs_dir or store.path.parent / "reviews") / uuid.uuid4().hex
            result = review_draft(store, args.draft_id, run_dir=directory, max_passes=args.max_passes)
        elif cmd == "search":
            result = store.search(args.query, limit=args.limit, **scope)
        elif cmd == "rebuild-index":
            result = store.rebuild_search()
        elif cmd == "index-jobs":
            result = store.index_jobs(args.after, args.limit)
        elif cmd == "events":
            result = store.events(args.after, args.limit)
        else:
            result = store.index_target(args.job_id)
        rendered = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            path = Path(args.output)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return 2 if isinstance(result, dict) and (result.get("valid") is False or result.get("status") == "needs_attention") else 0
    except (WikiError, OSError, sqlite3.Error, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
