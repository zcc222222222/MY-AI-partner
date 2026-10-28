"""Long-session probes in a temporary app DB. --live explicitly calls the configured LLM."""
import argparse
import hashlib
import json
import logging
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from sqlalchemy import create_engine, event, select, func
from sqlalchemy.orm import sessionmaker
from app import models
from app.agents.context_budget import input_cost
from app.agents.tools import TOOLS
from app.agents.roleplay import build_system_prompt
from app.services import chat_service, context_service
from app.migrations import migrate

CASES = [
    {"id": "ownership-correction", "initial": "我有爱弥斯，0链，没有她的专武。目标是大世界舒适体验，不追求竞速。",
     "correction": "更正：爱弥斯现在是1链了，仍没有专武；之前的大世界目标不变。",
     "question": "请简短复述我明确说过的角色、当前共鸣链、专武持有情况和玩法目标。不要推荐装备，也不要猜。"},
    {"id": "rover-identity", "initial": "你可以把提问的我当作漂泊者，这是对话身份。我还没提供游戏角色的形态或性别。",
     "correction": "接下来我想讨论气动漂泊者。我仍没说游戏里选的是男还是女。",
     "question": "现在能确定我想讨论哪种形态吗？你是否已经知道我的游戏角色性别？资料需要区分男女时应怎么处理？请简短回答。"},
    {"id": "version-switch", "initial": "我之前问的是3.7版本的爱弥斯机制。这是讨论的资料版本，不代表实时版本。",
     "correction": "接下来改问3.8版本。如果只有3.7资料，不要换回3.7来补答案。",
     "question": "我当前要求哪个版本？如果资料只有旧版本，你应该怎么处理？这里只回答我的要求，不用查询游戏机制。"},
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    records = []
    with tempfile.TemporaryDirectory() as folder:
        engine = create_engine("sqlite:///" + (Path(folder) / "app.db").as_posix())
        event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        migrate(engine)
        with sessionmaker(engine)() as db:
            db.add(models.User(id=1, username="probe", display_name="probe", password_hash="unused"))
            # The actual persona is used without changing the deployed DB/card.
            card = json.loads((Path(__file__).resolve().parents[1] / "backend/characters/amis.json").read_text(encoding="utf-8"))
            db.add(models.Character(id=1, slug="probe", name=card["name"], display_name=card["display_name"],
                                    personality=card["personality"], system_prompt=card["system_prompt"]))
            db.commit()
            for case in CASES:
                sid = case["id"]
                db.add(models.ChatSession(id=sid, character_id=1, owner_id=1, has_user_message=True))
                db.commit()
                db.add(models.Message(session_id=sid, role="user", content=case["initial"]))
                db.add(models.Message(session_id=sid, role="assistant", content="收到，后续以你明确提供的条件为准。"))
                for n in range(45):
                    db.add(models.Message(session_id=sid, role="user", content=(f"这是第{n}轮历史占位对话，没有增加持有角色、装备、形态或版本信息。" * 3)))
                    db.add(models.Message(session_id=sid, role="assistant", content="这是一段用于验证长会话裁剪的旧回复，不是游戏资料，也不提供玩家持有情况。" * 8))
                db.add(models.Message(session_id=sid, role="user", content=case["correction"]))
                db.add(models.Message(session_id=sid, role="assistant", content="已收到更正。"))
                db.commit()
                capture = {}
                actual = chat_service.run_agent
                def agent(messages, **kwargs):
                    capture["estimated_input_bytes"] = input_cost(messages, TOOLS)
                    raw = list(db.scalars(select(models.Message).where(models.Message.session_id == sid).order_by(models.Message.id)))
                    capture["unbounded_input_bytes"] = input_cost([messages[0], *[
                        {"role": m.role, "content": m.content} for m in raw]], TOOLS)
                    capture["system_sha256"] = hashlib.sha256(messages[0]["content"].encode()).hexdigest()
                    capture["context"] = messages[1:]
                    if args.live:
                        for entry in actual(messages, **kwargs):
                            if entry["type"] in ("usage", "generation"):
                                capture.setdefault("generation", []).append(entry)
                            yield entry
                    else:
                        yield {"type": "delta", "content": "offline transport stub"}
                selector_patch = patch.object(context_service, "select_memory", side_effect=(
                    context_service.select_memory if args.live else lambda c: [c[0]["message_id"], c[-1]["message_id"]]))
                with selector_patch, patch.object(chat_service, "run_agent", side_effect=agent):
                    events = [json.loads(e) for e in chat_service.stream_reply(db,
                        db.get(models.ChatSession, sid), db.get(models.Character, 1), case["question"])]
                answer = "".join(e.get("content", "") for e in events if e["type"] == "delta")
                final = next((e for e in events if e["type"] == "done"), None)
                cached = db.get(models.ContextSummary, sid)
                count = db.scalar(select(func.count()).select_from(models.Message).where(models.Message.session_id == sid))
                checks = {"complete": final is not None and final["status"] == "complete",
                          "summary_created": cached is not None, "originals_preserved": count == 96,
                          "initial_user_retained": case["initial"] in json.dumps(capture["context"], ensure_ascii=False),
                          "latest_correction_retained": case["correction"] in json.dumps(capture["context"], ensure_ascii=False),
                          "current_question_retained": capture["context"][-1]["content"] == case["question"]}
                records.append({"id": sid, "live": args.live, "checks": checks, "answer": answer,
                    "summary_format": cached.format_version if cached else None, **capture})
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps({"cases": records}, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({"id": sid, "checks": checks, "estimated_input_bytes": capture["estimated_input_bytes"]}), flush=True)
        engine.dispose()
    if not all(all(r["checks"].values()) for r in records):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
