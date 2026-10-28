"""Compare the public guide catalog with local, separately identified characters."""
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3

from .collector import chinese, utcnow
from .store import WikiError
from .updates import plan_documents, source_snapshot

CATALOG_URL = "https://guide-server.aki-game.com/role/avatar/list"


def catalog_rows(payload):
    if not isinstance(payload, dict) or payload.get("code") != 200 or not isinstance(payload.get("data"), list):
        raise WikiError("攻略角色目录格式无效")
    rows, seen = [], set()
    for item in payload["data"]:
        if not isinstance(item, dict):
            raise WikiError("攻略目录条目格式无效")
        role = str(item.get("roleGbId", ""))
        if not re.fullmatch(r"[0-9]{1,12}", role) or role in seen:
            raise WikiError("攻略目录含无效或重复角色ID")
        seen.add(role)
        name = (chinese(item.get("texts")) or {}).get("name", "").strip()
        if not name:
            continue
        peer = item.get("mayRoleGbId")
        rows.append({"role_id": role, "name": name, "catalog_peer_id": str(peer) if peer is not None else None})
    return rows


def refresh_catalog(client, path):
    payload = client.json(CATALOG_URL)
    # Validate before replacing the previous usable snapshot.
    catalog_rows(payload)
    from .updates import atomic_json
    snapshot = {"schema": 1, "fetched_at": utcnow(), "origin": CATALOG_URL, "payload": payload}
    atomic_json(Path(path), snapshot)
    return snapshot


def load_catalog(path):
    snapshot = json.loads(Path(path).read_text(encoding="utf-8"))
    if snapshot.get("schema") != 1 or snapshot.get("origin") != CATALOG_URL:
        raise WikiError("目录快照格式无效，请刷新目录")
    catalog_rows(snapshot["payload"])
    return snapshot


def coverage(store, knowledge_db, snapshot):
    catalog = {r["role_id"]: r for r in catalog_rows(snapshot["payload"])}
    with closing(sqlite3.connect(Path(knowledge_db).resolve().as_uri() + "?mode=ro", uri=True)) as c:
        roster = {str(r[0]): {"role_id": str(r[0]), "name": r[1], "element": r[2]}
                  for r in c.execute("SELECT id,name,element_name FROM characters ORDER BY id")}
    plan = plan_documents(store)
    jobs = {}
    for job in plan["pages"]:
        jobs.setdefault(job["source_id"], []).append(job)
    sources = {}
    for sid in source_snapshot(store):
        match = re.fullmatch(r"mcguide/([0-9]+)/([0-9]+)", sid)
        if match:
            sources.setdefault(match[1], []).append(sid)
    rows = []
    for role, item in roster.items():
        entry = catalog.get(role)
        imported = sources.get(role, [])
        current = [sid for sid in imported if jobs.get(sid) and all(j["status"] == "unchanged" for j in jobs[sid])]
        identity_matches = bool(entry and entry["name"] == item["name"])
        state = ("catalog_identity_mismatch" if entry and not identity_matches else
                 "current" if current else "pending" if imported else
                 "catalog_only" if entry else "not_in_catalog")
        # Catalog peer linkage is visible, but never silently shares source tags or claims.
        peer = next((r["role_id"] for r in catalog.values() if r.get("catalog_peer_id") == role
                     and r["role_id"] in roster and roster[r["role_id"]]["name"] == item["name"]
                     and roster[r["role_id"]]["element"] == item["element"]), None)
        rows.append({**item, "status": state, "catalog_name": entry["name"] if entry else None,
                     "catalog_entry_via_peer": peer, "imported_guides": len(imported),
                     "current_guides": len(current), "pending_guides": len(imported) - len(current),
                     "source_ids": sorted(imported)})
    return {"generated_at": utcnow(), "catalog_fetched_at": snapshot["fetched_at"],
            "note": "current表示至少一篇当前已审核攻略，不表示作者、版本或玩法覆盖完整；目录存在不证明有中文攻略。男女记录不自动共享攻略。",
            "summary": {"local_records": len(rows), "catalog_entries": len(catalog),
                        "current_records": sum(r["status"] == "current" for r in rows),
                        "pending_records": sum(r["status"] == "pending" for r in rows),
                        "catalog_only_records": sum(r["status"] == "catalog_only" for r in rows),
                        "peer_only_records": sum(r["status"] == "not_in_catalog" and bool(r["catalog_entry_via_peer"]) for r in rows)},
            "characters": rows, "catalog_without_local_record": [r for key,r in catalog.items() if key not in roster],
            "planning_errors": plan["errors"]}


def select_batch(report, size=3):
    if type(size) is not int or not 1 <= size <= 5:
        raise WikiError("每批角色数须为1—5")
    # Finish staged work before new collection; then fill missing Rover forms first.
    candidates = [r for r in report["characters"] if r["status"] in ("pending", "catalog_only")]
    candidates.sort(key=lambda r: (r["status"] != "pending", not r["name"].startswith("漂泊者·"), int(r["role_id"])))
    return [r["role_id"] for r in candidates[:size]]
