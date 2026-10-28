"""Local retrieval acceptance for the first three roster pages. No LLM/network."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from llmwiki.tools import search_wiki, MAX_RESULT_CHARS

CASES = [
    ("维里奈", "1503", "光合能量", ["上限4层", "第5段普攻"]),
    ("维里奈", "1503", "光合标记", ["每秒可触发1次", "命中"]),
    ("维里奈", "1503", "生命的恩惠", ["10分钟", "120%"]),
    ("维里奈", "1503", "共鸣链4", ["15%", "24秒", "【共鸣链4】"]),
    ("守岸人", "1505", "深潜星域", ["0.2%", "12.5%"]),
    ("守岸人", "1505", "解限星域", ["0.1%", "25%"]),
    ("守岸人", "1505", "变奏技能洞悉", ["必定暴击", "同一个解限星域"]),
    ("守岸人", "1505", "共鸣链1", ["10秒", "不再结束星域", "【共鸣链1】"]),
    ("莫宁", "1209", "干涉标记", ["目标拥有莫宁施加的干涉标记", "0.25%", "上限40%"]),
    ("莫宁", "1209", "有界性证明", ["生效期间", "30%"]),
    ("莫宁", "1209", "递归", ["25%", "30秒"]),
    ("莫宁", "1209", "共鸣链1", ["150%", "【共鸣链1】"]),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    results = []
    for name, ident, query, expected in CASES:
        result = search_wiki(query, name, game_version="3.7", kind="character_mechanics")
        text = "\n".join(item["snippet"] for item in result["results"])
        missing = [term for term in expected if term not in text]
        passed = result["status"] == "found" and not missing and all(
            item["page_id"] == f"character/{ident}/mechanics" for item in result["results"])
        passed = passed and len(json.dumps(result, ensure_ascii=False)) <= MAX_RESULT_CHARS
        results.append({"name": name, "query": query, "passed": passed, "missing": missing, "result": result})
    for name, ident in [("维里奈", "1503"), ("守岸人", "1505"), ("莫宁", "1209")]:
        result = search_wiki("共鸣链", name, game_version="3.8", kind="character_mechanics")
        results.append({"name": name, "query": "version_isolation_3.8", "passed": not result["results"], "result": result})
    report = {"passed": sum(r["passed"] for r in results), "total": len(results), "cases": results,
              "scope": "固定本地检索回归，非真实模型答复或独立玩家正确率"}
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "total": report["total"],
                      "failures": [{"name": r["name"], "query": r["query"], "missing": r.get("missing")} for r in results if not r["passed"]]}, ensure_ascii=False))
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
