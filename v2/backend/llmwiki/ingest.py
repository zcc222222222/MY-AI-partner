"""Adapters keep original bytes; parsers for PDF/Word can be added separately."""
from contextlib import closing
from pathlib import Path
import os
import sqlite3

from knowledge.config import DB_PATH
from .store import MAX_SOURCE_BYTES, WikiError


def import_file(store, path, *, source_id, game_version, locale="zh", title=None, origin=None, entities=None, topics=None):
    path = Path(path).resolve()
    media_type = {".md": "text/markdown", ".txt": "text/plain", ".json": "application/json"}.get(path.suffix.lower())
    if media_type is None:
        raise WikiError("当前仅接受.md/.txt/.json；其他文档须先接解析器")
    with path.open("rb") as source_file:
        raw = source_file.read(MAX_SOURCE_BYTES + 1)
    return store.import_source(source_id, game_version=game_version, locale=locale,
                               title=title or path.stem, raw=raw, media_type=media_type,
                               origin=origin if origin is not None else str(path),
                               metadata={"entities": entities or [], "topics": topics or []})


def sync_nanoka(store, *, kind, doc_id, game_version, locale="zh", knowledge_path=None):
    path = Path(knowledge_path or DB_PATH).resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM raw_docs WHERE kind=? AND doc_id=? AND game_version=? AND (locale=? OR locale='')",
                            (kind, str(doc_id), game_version, locale)).fetchall()
        if len(rows) != 1:
            raise WikiError("没有找到唯一的本地nanoka镜像记录，请确认kind、id、版本及语言")
        row = dict(rows[0])
    etype = {"character": "character", "weapon_detail": "weapon", "echo_detail": "echo"}.get(kind)
    return store.import_source(f"nanoka/{kind}/{doc_id}", game_version=game_version, locale=locale,
                               title=f"nanoka {kind}/{doc_id}", origin=row["source_url"] or "", media_type="application/json",
                               raw=row["payload"].encode("utf-8"),
                               metadata={"entities": [f"{etype}:{doc_id}"] if etype else [], "topics": [],
                                         "upstream_sha256": row["sha256"], "upstream_db": str(path), "adapter": "nanoka_raw_docs"})


def refresh_nanoka(store, *, game_version, locale="zh", check_only=False):
    """Refresh already subscribed sources only; never crawl or import the whole roster.

    Another game version in the upstream database does not erase historical snapshots.
    Missing/inaccessible same-version upstream data fails closed until repaired.
    check_only validates hashes without importing revisions or dirtying pages.
    """
    import json
    with store.connection() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT r.source_id,r.metadata FROM sources s JOIN source_revisions r USING(source_id,game_version,locale) "
            "WHERE s.game_version=? AND s.locale=? AND s.current_revision=r.revision AND r.deleted=0",
            (game_version, locale))]
    results, snapshots = [], {}
    for row in rows:
        metadata = json.loads(row["metadata"])
        if metadata.get("adapter") != "nanoka_raw_docs" or "upstream_sha256" not in metadata:
            continue
        # Explicit deployment mapping preserves immutable source metadata when
        # moving a snapshot from a Windows host into a Linux container.
        path = Path(os.getenv('WIKI_UPSTREAM_DB_PATH') or metadata.get("upstream_db") or DB_PATH).resolve()
        try:
            _, kind, doc_id = row["source_id"].split("/", 2)
            if path not in snapshots:
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
                    records = conn.execute("SELECT kind,doc_id,sha256 FROM raw_docs WHERE game_version=? AND (locale=? OR locale='')",
                                           (game_version, locale)).fetchall()
                    has_version = bool(conn.execute("SELECT 1 FROM raw_docs WHERE game_version=? LIMIT 1", (game_version,)).fetchone())
                by_id = {}
                for raw_kind, raw_id, checksum in records:
                    by_id.setdefault((raw_kind, str(raw_id)), []).append(checksum)
                snapshots[path] = by_id, has_version
            by_id, has_version = snapshots[path]
            latest = by_id.get((kind, doc_id), [])
            if not latest:
                if not has_version:
                    continue
                raise WikiError("upstream_missing_source: " + row["source_id"])
            if len(latest) != 1:
                raise WikiError("upstream_ambiguous_source: " + row["source_id"])
            changed = latest[0] != metadata["upstream_sha256"]
            if changed:
                if check_only:
                    raise WikiError("upstream_changed: " + row["source_id"] + "；请通过知识库更新任务同步并重新审核")
                results.append(sync_nanoka(store, kind=kind, doc_id=doc_id, game_version=game_version,
                                           locale=locale, knowledge_path=path))
        except (sqlite3.Error, OSError, ValueError) as exc:
            if isinstance(exc, WikiError):
                raise
            raise WikiError("upstream_unavailable: " + row["source_id"]) from None
    return {"refreshed": len(results), "sources": results}
