"""Offline full-roster coverage and per-ID retrieval/variant isolation acceptance."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from llmwiki import WikiStore
from llmwiki.roster import plan_roster
from llmwiki.tools import search_wiki, MAX_RESULT_CHARS
from knowledge.config import DB_PATH
from knowledge.relations import resolve


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    store = WikiStore()
    plan = plan_roster(store, game_version=args.version)
    checks = []
    for entry in plan["entries"]:
        result = {"id": entry["character_id"], "name": entry["display_name"], "passed": False}
        if entry["status"] == "current":
            page = store.get_page(entry["page_id"], game_version=args.version)["page"]
            query = page["claims"][0]["text"][:100]
            found = search_wiki(query, entry["character_id"], game_version=args.version, kind="character_mechanics")
            result.update(query=query, status=found.get("status"), claim_count=len(page["claims"]),
                          passed=found.get("status") == "found" and bool(found["results"]) and
                          all(p["page_id"] == entry["page_id"] for p in found["results"]) and
                          page["entities"] == [entry["entity"]] and
                          len(json.dumps(found, ensure_ascii=False)) <= MAX_RESULT_CHARS)
        else:
            result["status"] = entry["status"]
        checks.append(result)
    with closing(sqlite3.connect(Path(DB_PATH).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        for entry in plan["entries"]:
            if not entry["name"].startswith("漂泊者"):
                continue
            resolved = resolve(conn, "character", f"{entry['name']}·{entry['sex']}")
            checks.append({"id": entry["character_id"], "kind": "rover_explicit_variant", "passed": str(resolved.get("id")) == entry["character_id"]})
        for name in sorted({e["name"] for e in plan["entries"] if e["name"].startswith("漂泊者")}):
            ambiguous = resolve(conn, "character", name)
            checks.append({"name": name, "kind": "rover_ambiguity", "passed": "error" in ambiguous and ambiguous.get("total_candidates") == 2})
        checks.append({"kind": "user_identity_is_not_character", "passed": "error" in resolve(conn, "character", "我")})
    report = {"version": args.version, "coverage": plan["counts"], "passed": sum(c["passed"] for c in checks), "total": len(checks),
              "checks": checks, "scope": "本地实体与已审核页面检索检查，不是独立游戏实测或端到端模型正确率"}
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "checks"}, ensure_ascii=False))
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
