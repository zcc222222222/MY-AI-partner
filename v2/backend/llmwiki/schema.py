"""Authoritative wiki state and rebuildable search index live in one transaction."""
from functools import lru_cache
import sqlite3


DDL = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources(
 source_id TEXT NOT NULL, game_version TEXT NOT NULL, locale TEXT NOT NULL,
 current_revision INTEGER NOT NULL,
 PRIMARY KEY(source_id,game_version,locale));
CREATE TABLE IF NOT EXISTS source_revisions(
 source_id TEXT NOT NULL, game_version TEXT NOT NULL, locale TEXT NOT NULL,
 revision INTEGER NOT NULL, title TEXT NOT NULL, origin TEXT NOT NULL,
 media_type TEXT NOT NULL, content_hash TEXT NOT NULL, fingerprint TEXT NOT NULL,
 raw BLOB, text TEXT, metadata TEXT NOT NULL, deleted INTEGER NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(source_id,game_version,locale,revision),
 FOREIGN KEY(source_id,game_version,locale) REFERENCES sources);
CREATE TABLE IF NOT EXISTS pages(
 page_id TEXT NOT NULL, game_version TEXT NOT NULL, locale TEXT NOT NULL,
 active_revision INTEGER NOT NULL, dirty INTEGER NOT NULL, reasons TEXT NOT NULL,
 PRIMARY KEY(page_id,game_version,locale));
CREATE TABLE IF NOT EXISTS page_revisions(
 page_id TEXT NOT NULL, game_version TEXT NOT NULL, locale TEXT NOT NULL,
 revision INTEGER NOT NULL, payload TEXT NOT NULL, fingerprint TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(page_id,game_version,locale,revision));
CREATE TABLE IF NOT EXISTS source_dependencies(
 page_id TEXT NOT NULL, game_version TEXT NOT NULL, locale TEXT NOT NULL,
 page_revision INTEGER NOT NULL, source_id TEXT NOT NULL, source_revision INTEGER NOT NULL,
 PRIMARY KEY(page_id,game_version,locale,page_revision,source_id));
CREATE TABLE IF NOT EXISTS page_dependencies(
 page_id TEXT NOT NULL, game_version TEXT NOT NULL, locale TEXT NOT NULL,
 page_revision INTEGER NOT NULL, dependency_id TEXT NOT NULL, dependency_revision INTEGER NOT NULL,
 PRIMARY KEY(page_id,game_version,locale,page_revision,dependency_id));
CREATE TABLE IF NOT EXISTS page_tags(
 page_id TEXT NOT NULL, game_version TEXT NOT NULL, locale TEXT NOT NULL,
 tag_type TEXT NOT NULL, value TEXT NOT NULL,
 PRIMARY KEY(page_id,game_version,locale,tag_type,value));
CREATE TABLE IF NOT EXISTS drafts(
 draft_id TEXT PRIMARY KEY, page_id TEXT NOT NULL, game_version TEXT NOT NULL,
 locale TEXT NOT NULL, payload TEXT NOT NULL, fingerprint TEXT NOT NULL,
 state TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
 game_version TEXT NOT NULL, locale TEXT NOT NULL, payload TEXT NOT NULL,
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reviews(
 review_id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id TEXT NOT NULL,
 fingerprint TEXT NOT NULL, decision TEXT NOT NULL, reviewer TEXT NOT NULL,
 reviewer_type TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL,
 FOREIGN KEY(draft_id) REFERENCES drafts(draft_id));
CREATE TABLE IF NOT EXISTS index_jobs(
 job_id INTEGER PRIMARY KEY AUTOINCREMENT, page_id TEXT NOT NULL,
 game_version TEXT NOT NULL, locale TEXT NOT NULL, revision INTEGER NOT NULL,
 operation TEXT NOT NULL, generation INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS source_dependents ON source_dependencies(source_id,game_version,locale);
CREATE INDEX IF NOT EXISTS page_dependents ON page_dependencies(dependency_id,game_version,locale);
CREATE INDEX IF NOT EXISTS tag_lookup ON page_tags(tag_type,value,game_version,locale);
CREATE TABLE IF NOT EXISTS document_images(
 image_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, game_version TEXT NOT NULL,
 locale TEXT NOT NULL, source_revision INTEGER NOT NULL, payload TEXT NOT NULL,
 FOREIGN KEY(source_id,game_version,locale,source_revision)
 REFERENCES source_revisions(source_id,game_version,locale,revision));
CREATE INDEX IF NOT EXISTS image_source ON document_images(source_id,game_version,locale,source_revision);
CREATE TRIGGER IF NOT EXISTS immutable_sources_update BEFORE UPDATE ON source_revisions
 BEGIN SELECT RAISE(ABORT,'source revisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_sources_delete BEFORE DELETE ON source_revisions
 BEGIN SELECT RAISE(ABORT,'source revisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_pages_update BEFORE UPDATE ON page_revisions
 BEGIN SELECT RAISE(ABORT,'page revisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS immutable_pages_delete BEFORE DELETE ON page_revisions
 BEGIN SELECT RAISE(ABORT,'page revisions are immutable'); END;
"""


@lru_cache(maxsize=1)
def required_columns():
    """Expected table columns, derived from the maintenance schema in memory."""
    conn = sqlite3.connect(':memory:')
    try:
        conn.executescript(DDL)
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {table: frozenset(row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')) for table in tables}
    finally:
        conn.close()
