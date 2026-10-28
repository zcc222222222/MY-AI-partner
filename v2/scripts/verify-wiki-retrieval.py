"""Local retrieval acceptance; requires the published, reviewed Aemeath sample."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from llmwiki import WikiStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db")
    parser.add_argument("--output")
    args = parser.parse_args()
    fixture = json.loads((ROOT / "tests/fixtures/wiki/retrieval-cases.json").read_text(encoding="utf-8"))
    store = WikiStore(args.db)
    results = []
    for case in fixture["cases"]:
        hits = store.search(case["query"], game_version=fixture["game_version"], locale=fixture["locale"],
                            entities=fixture["entities"], kind=fixture["kind"], reviewed_only=True)
        text = "\n".join(hit["snippet"] for hit in hits)
        size = len(json.dumps(hits, ensure_ascii=False))
        results.append({**case, "passed": case["expected"] in text and size <= 8000,
                        "chars": size, "hits": hits})
    report = {"note": fixture["note"], "entities": fixture["entities"], "kind": fixture["kind"], "passed": sum(case["passed"] for case in results),
              "total": len(results), "max_chars": max(case["chars"] for case in results), "cases": results}
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Retrieval acceptance {report['passed']}/{report['total']}; maximum result {report['max_chars']} characters")
    for case in results:
        if not case["passed"]:
            print("FAIL " + case["query"])
    return 0 if report["passed"] == report["total"] else 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
