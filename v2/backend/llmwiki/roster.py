"""Local roster coverage and small, resumable compilation batches. No auto-publication."""
from contextlib import closing
from collections import Counter
import json
from pathlib import Path
import sqlite3
import time

from knowledge.config import DB_PATH
from .compiler import compile_page
from .ingest import sync_nanoka
from .store import WikiError


def plan_roster(store, *, game_version, locale="zh", knowledge_path=None):
    path = Path(knowledge_path or DB_PATH).resolve()
    store.refresh(game_version=game_version, locale=locale)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        characters = conn.execute("SELECT id,name FROM characters WHERE game_version=? ORDER BY id", (game_version,)).fetchall()
        raw = conn.execute("SELECT doc_id,sha256 FROM raw_docs WHERE kind='character' AND game_version=? AND (locale=? OR locale='')",
                           (game_version, locale)).fetchall()
        sexes = dict(conn.execute("SELECT character_id,sex FROM character_profiles")) if conn.execute("SELECT 1 FROM sqlite_master WHERE name='character_profiles'").fetchone() else {}
    if not characters:
        raise WikiError("指定版本没有本地角色记录；不会把其他版本的数据改标签使用")
    sources = {}
    for row in raw:
        sources.setdefault(str(row["doc_id"]), []).append(row["sha256"])
    pages = {}
    with store.connection() as conn:
        rows = conn.execute("SELECT p.page_id,p.dirty,p.active_revision,r.payload, "
                            "EXISTS (SELECT 1 FROM drafts d JOIN reviews v ON v.draft_id=d.draft_id AND v.fingerprint=d.fingerprint "
                            "WHERE d.page_id=p.page_id AND d.game_version=p.game_version AND d.locale=p.locale "
                            "AND d.fingerprint=r.fingerprint AND d.state='published' "
                            "AND v.review_id=(SELECT MAX(review_id) FROM reviews WHERE draft_id=d.draft_id) AND v.decision='approved') AS reviewed "
                            "FROM pages p JOIN page_revisions r "
                            "ON p.page_id=r.page_id AND p.game_version=r.game_version AND p.locale=r.locale AND p.active_revision=r.revision "
                            "WHERE p.game_version=? AND p.locale=?", (game_version, locale)).fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            if payload.get("kind") != "character_mechanics":
                continue
            for entity in payload.get("entities", []):
                if entity.startswith("character:"):
                    if entity in pages:
                        raise WikiError("同一角色存在多篇机制总览，请先明确主页面：" + entity)
                    pages[entity] = dict(row)
    entries = []
    for character in characters:
        doc_id, name = str(character["id"]), character["name"]
        entity = "character:" + doc_id
        existing = pages.get(entity)
        checksums = sources.get(doc_id, [])
        status = "missing_source" if not checksums else "ambiguous_source" if len(checksums) != 1 else "current" if existing and not existing["dirty"] and existing["reviewed"] else "dirty" if existing else "missing_page"
        sex = sexes.get(character["id"])
        display_name = f"{name}（{sex}）" if name.startswith("漂泊者") and sex in ("男", "女") else name
        entries.append({"character_id": doc_id, "name": name, "display_name": display_name, "sex": sex, "entity": entity, "status": status,
                        "page_id": existing["page_id"] if existing else f"character/{doc_id}/mechanics",
                        "source_sha256": checksums[0] if len(checksums) == 1 else None,
                        "active_revision": existing["active_revision"] if existing else 0})
    return {"game_version": game_version, "locale": locale, "knowledge_db": str(path),
            "counts": dict(Counter(e["status"] for e in entries)), "total": len(entries), "entries": entries}


def compile_roster(store, *, game_version, locale="zh", knowledge_path=None, character_ids=None,
                   limit=3, report_path, compile_fn=None, pause=time.sleep):
    if type(limit) is not int or not 1 <= limit <= 5:
        raise WikiError("每批仅允许1–5篇，请先审核本批结果再继续")
    plan = plan_roster(store, game_version=game_version, locale=locale, knowledge_path=knowledge_path)
    wanted = {str(i) for i in character_ids} if character_ids else None
    if wanted and wanted - {e["character_id"] for e in plan["entries"]}:
        raise WikiError("选择的角色不在指定版本的本地角色名单中")
    selected = [e for e in plan["entries"] if (wanted is None or e["character_id"] in wanted)
                and e["status"] in {"missing_page", "dirty"}][:limit]
    report = {"game_version": game_version, "locale": locale, "selected": selected, "results": [],
              "status": "running", "auto_publish": False}
    path = Path(report_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    def save():
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    save()
    for index, entry in enumerate(selected):
        if index:
            pause(2)
        try:
            with closing(sqlite3.connect(Path(plan["knowledge_db"]).as_uri() + "?mode=ro", uri=True)) as conn:
                checksums = conn.execute("SELECT sha256 FROM raw_docs WHERE kind='character' AND doc_id=? AND game_version=? AND (locale=? OR locale='')",
                                         (entry["character_id"], game_version, locale)).fetchall()
            if checksums != [(entry["source_sha256"],)]:
                raise WikiError("本地原文在计划后发生变化，跳过并重新生成计划")
            sync_nanoka(store, kind="character", doc_id=entry["character_id"], game_version=game_version,
                        locale=locale, knowledge_path=plan["knowledge_db"])
            result = (compile_fn or compile_page)(store, entry["page_id"], game_version=game_version, locale=locale,
                title=entry["display_name"] + "机制总览", entities=[entry["entity"]], topics=["mechanics"], kind="character_mechanics", publish=False)
        except (WikiError, sqlite3.Error, OSError) as exc:
            result = {"valid": False, "status": "failed", "errors": [str(exc)]}
        report["results"].append({**entry, "compile": result})
        save()
    report["status"] = "awaiting_review" if all(r["compile"].get("valid") for r in report["results"]) else "partial_failure"
    if not selected:
        report["status"] = "nothing_to_compile"
    save()
    return report
