"""Bounded guide retrieval checks; --live additionally calls the chat model, without saving chats."""
import argparse
import json
import logging
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from llmwiki.tools import search_wiki
from app.agents.orchestrator import run_agent
from app.agents.roleplay import build_system_prompt
from app.agents.tools import TOOLS

CASES = [
    ("substitute", "莫宁 替代", "3.7", "我有爱弥斯、琳奈和守岸人，没有莫宁。按现有3.7攻略，这三个人能否组成震谐队？先说资料支持什么，别替我假定链数或专武。"),
    ("weapon", "千古洑流 武器", "3.7", "我的爱弥斯没有专武，只有谐振1阶千古洑流。按现有3.7攻略能先用吗？不要算不存在的提升百分比，请带上计算前提。"),
    ("fusion", "聚爆 配队", "3.7", "我有爱弥斯、达妮娅和千咲，想了解现有3.7聚爆配队攻略。说清楚来源观点和它的计算版本，不要编输出轴。"),
    ("upgrade", "技能 升级", "3.7", "我爱弥斯的养成资源有限。现有3.7攻略里技能升级先后顺序是什么？请标明资料适用范围。"),
    ("missing-version", "配队", "3.8", "只问3.8版本的爱弥斯配队。如果库里只有3.7攻略，请直接说明没有3.8依据，不要切回3.7回答。"),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--case", action="append", choices=[c[0] for c in CASES], dest="cases")
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    card = json.loads((Path(__file__).resolve().parents[1] / "backend/characters/amis.json").read_text(encoding="utf-8"))
    system = build_system_prompt(SimpleNamespace(**card))
    reports = []
    for name, query, version, question in CASES:
        if args.cases and name not in args.cases:
            continue
        payload = search_wiki(query, "1210", game_version=version, kind="team_guide")
        found = payload.get("status") == "found"
        checks = {"expected_scope": found if version == "3.7" else payload.get("status") == "no_reviewed_match",
                  "bounded_payload": len(json.dumps(payload, ensure_ascii=False)) <= 8000,
                  "correct_kind": all(p["kind"] == "team_guide" for p in payload.get("results", [])),
                  "version_notes": all("3.7" in m["text"] and "3.6" in m["text"] for p in payload.get("results", []) for m in p["matches"])}
        record = {"id": name, "checks": checks, "retrieval": payload, "question": question}
        if args.live:
            answer, traces, usage = [], [], []
            for e in run_agent([{"role": "system", "content": system}, {"role": "user", "content": question}], tools=TOOLS):
                if e["type"] == "delta": answer.append(e["content"])
                elif e["type"] == "knowledge_trace": traces.append(e["trace"])
                elif e["type"] == "usage": usage.append(e["usage"])
            checks["live_retrieval_used"] = any(t.get("selected") for t in traces) if version == "3.7" else any(t.get("game_version") == "3.8" and not t.get("selected") for t in traces)
            checks["live_no_version_switch"] = all(t.get("game_version") in (None, version) for t in traces)
            record.update({"answer": "".join(answer), "traces": traces, "usage": usage})
        reports.append(record)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"live": args.live, "cases": reports}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"id": name, "checks": checks}), flush=True)
    if not all(all(r["checks"].values()) for r in reports):
        raise SystemExit(1)


if __name__ == "__main__": main()
