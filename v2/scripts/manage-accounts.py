"""Local administrator only: migrations and explicit assignment of legacy chats."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from sqlalchemy import select, update
from app.database import SessionLocal, engine
from app.migrations import migrate
from app.models import ChatSession, User


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    commands.add_parser("list-unassigned")
    assign = commands.add_parser("assign")
    assign.add_argument("--username", required=True)
    assign.add_argument("--session-id", action="append", required=True)
    assign.add_argument("--apply", action="store_true", help="Without this flag, preview only")
    args = parser.parse_args()
    if args.command == "migrate":
        print(json.dumps(migrate(engine), ensure_ascii=True))
        return
    with SessionLocal() as db:
        if args.command == "list-unassigned":
            rows = db.scalars(select(ChatSession).where(ChatSession.owner_id.is_(None))).all()
            print(json.dumps([{"session_id": r.id, "title": r.title} for r in rows], ensure_ascii=True))
            return
        user = db.scalar(select(User).where(User.username == args.username.lower(), User.is_active.is_(True)))
        if user is None:
            parser.error("Active target account not found; register it first")
        ids = set(args.session_id)
        rows = db.scalars(select(ChatSession).where(ChatSession.id.in_(ids), ChatSession.owner_id.is_(None))).all()
        if {r.id for r in rows} != ids:
            parser.error("Some sessions are missing or already assigned; no changes applied")
        if args.apply:
            changed = db.execute(update(ChatSession).where(ChatSession.id.in_(ids), ChatSession.owner_id.is_(None)).values(owner_id=user.id)).rowcount
            if changed != len(ids):
                db.rollback()
                parser.error("Ownership changed concurrently; no changes applied")
            db.commit()
        print(json.dumps({"applied": args.apply, "username": user.username, "session_ids": sorted(ids)}, ensure_ascii=True))


if __name__ == "__main__":
    try:
        main()
    finally:
        engine.dispose()
