"""Explicit schema migrations with a SQLite backup before changing existing data."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import uuid

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

HEAD = "0008_guest_access"
ALLOWED_TABLES = {"characters", "chat_sessions", "messages", "users", "auth_sessions", "auth_throttles", "alembic_version", "email_verifications", "password_reset_challenges", "chat_context_summaries", "quota_buckets", "generation_requests", "llm_usage"}


def migrate(engine, target="head"):
    with engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
        if tables - ALLOWED_TABLES:
            raise RuntimeError("Target is not the application database; refusing schema changes")
        current = conn.scalar(text("SELECT version_num FROM alembic_version")) if "alembic_version" in tables else None
        if (target == "head" and current == HEAD) or target == current:
            return {"revision": current, "changed": False, "backup": None}
        if "chat_sessions" in tables and "owner_id" not in {c["name"] for c in inspect(conn).get_columns("chat_sessions")}:
            for table, required in (("characters", {"id", "slug", "system_prompt"}), ("chat_sessions", {"id", "character_id", "title"}), ("messages", {"id", "session_id", "role", "content"})):
                if table not in tables or not required.issubset({c["name"] for c in inspect(conn).get_columns(table)}):
                    raise RuntimeError("Legacy schema does not match; no changes applied")
    backup = None
    if tables and engine.dialect.name == "sqlite" and engine.url.database != ":memory:":
        source = Path(engine.url.database).resolve()
        folder = source.parent / "backups"
        folder.mkdir(exist_ok=True)
        destination = folder / ("app-before-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8] + ".db")
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src, closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)
        backup = str(destination)
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "migrations"))
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        if target == "head":
            command.upgrade(config, target)
        else:
            command.downgrade(config, target)
    return {"revision": HEAD if target == "head" else target, "changed": True, "backup": backup}
