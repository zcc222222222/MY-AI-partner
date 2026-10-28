"""Run real chat integration probes only with explicit --live; no app.db writes."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from dotenv import load_dotenv
load_dotenv(ROOT / "backend/.env")
from app.agents import orchestrator, tools
from app.agents.roleplay import build_system_prompt
from app.services.chat_service import _source_suffix
from app.config import settings


def service_probe(output):
    """Exercise real persistence and SSE assembly in an isolated in-memory DB."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.database import Base
    from app.seed import seed_characters
    from app.services import chat_service, session_service
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    report = {"mode": "real-service-with-isolated-memory-db", "turns": []}
    original = chat_service.run_agent
    try:
        with Session(engine) as db:
            seed_characters(db)
            from app.models import User
            db.add(User(id=1, username="verify", display_name="Verify", password_hash="unused"))
            db.commit()
            session = session_service.create_session(db, owner_id=1)
            character = session_service.get_default_character(db)
            questions = ["爱弥斯的光翼共奏怎么触发？简短说明两种形态的条件。", "那机兵形态放完后会切换成什么形态？"]
            for question in questions:
                item = {"query": question, "usage": [], "traces": []}
                report["turns"].append(item)
                def events(messages, **kwargs):
                    item["system_sha256"] = hashlib.sha256(messages[0]["content"].encode()).hexdigest()
                    for event in original(messages, **kwargs):
                        if event["type"] == "usage":
                            item["usage"].append(event["usage"])
                        elif event["type"] == "knowledge_trace":
                            item["traces"].append(event["trace"])
                        yield event
                with patch.object(chat_service, "run_agent", side_effect=events):
                    streamed = [json.loads(event) for event in chat_service.stream_reply(db, session, character, question)]
                history = session_service.list_messages(db, session.id, owner_id=1)
                item["answer"] = history[-1].content
                item["done"] = next((event for event in streamed if event["type"] == "done"), None)
                item["status"] = "complete" if item["done"] and item["done"]["status"] == "complete" else "failed"
                assembled = "".join(event["content"] for event in streamed if event["type"] == "delta")
                item["stream_matches_persistence"] = assembled == item["answer"]
                output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print("SERVICE " + item["status"], flush=True)
            report["message_count"] = len(session_service.list_messages(db, session.id, owner_id=1))
            report["system_stable"] = len({item["system_sha256"] for item in report["turns"]}) == 1
    finally:
        engine.dispose()
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["system_stable"] and report["message_count"] == 4 and all(item["status"] == "complete" and item["stream_matches_persistence"] for item in report["turns"]) else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="调用配置的付费模型；不启动服务器、不写聊天数据库")
    parser.add_argument("--output", required=True)
    parser.add_argument("--case", action="append", help="仅执行指定用例id")
    parser.add_argument("--service", action="store_true", help="在内存会话库中运行两轮真实stream_reply，验证SSE与持久化")
    args = parser.parse_args()
    if not args.live:
        parser.error("真实模型验收需显式传入--live；离线回归请运行unittest")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.service:
        return service_probe(output)
    card = json.loads((ROOT / "backend/characters/amis.json").read_text(encoding="utf-8"))
    system = build_system_prompt(SimpleNamespace(**card))
    fixture = json.loads((ROOT / "tests/fixtures/wiki/chat-cases.json").read_text(encoding="utf-8"))
    report = {"note": fixture["note"], "model": settings.deepseek_model,
              "system_sha256": hashlib.sha256(system.encode()).hexdigest(), "cases": []}
    original = tools.execute_tool
    for case in fixture["cases"]:
        if args.case and case["id"] not in args.case:
            continue
        item = {**case, "status": "running", "answer": "", "tool_calls": [], "usage": [], "traces": [], "tool_metrics": []}
        report["cases"].append(item)
        print("START " + case["id"], flush=True)
        sources, text = [], ""
        started = time.monotonic()
        def execute(name, arguments):
            result = original(name, arguments)
            parsed = json.loads(result)
            item["tool_calls"].append({"name": name, "arguments": json.loads(arguments or "{}"),
                                       "error": parsed.get("error"), "status": parsed.get("status"),
                                       "result_chars": len(result)})
            return result
        try:
            with patch.object(orchestrator, "execute_tool", side_effect=execute):
                for event in orchestrator.run_agent([{"role": "system", "content": system}, {"role": "user", "content": case["query"]}],
                                                     tools=tools.TOOLS, temperature=settings.llm_temperature, max_tokens=settings.llm_max_tokens):
                    if event["type"] == "delta":
                        text += event["content"]
                    elif event["type"] == "usage":
                        item["usage"].append(event["usage"])
                    elif event["type"] == "knowledge_trace":
                        item["traces"].append(event["trace"])
                        sources.extend(event["sources"])
                    elif event["type"] == "tool_metrics":
                        item["tool_metrics"].append(event)
            item.update(status="complete", answer=text + _source_suffix(sources, text))
        except Exception as exc:
            item.update(status="failed", error=type(exc).__name__, reason=getattr(exc, "reason", None), partial_chars=len(text))
        item["seconds"] = round(time.monotonic() - started, 3)
        item["output_tokens"] = sum(u.get("completion_tokens", 0) for u in item["usage"])
        item["prompt_cache_hit_tokens"] = sum(u.get("prompt_cache_hit_tokens", 0) for u in item["usage"])
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"DONE {case['id']} {item['status']} calls={len(item['tool_calls'])} tokens={item['output_tokens']}", flush=True)
    return 0 if all(case["status"] == "complete" for case in report["cases"]) else 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
