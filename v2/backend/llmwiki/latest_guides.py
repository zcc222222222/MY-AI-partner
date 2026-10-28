"""Checkpointed full catalog sync: one latest Chinese guide per catalog character."""
import json
from pathlib import Path
import re
import uuid

from .collector import DOCUMENT_SCOPE, utcnow
from .guide_coverage import catalog_rows, coverage
from .store import WikiError
from .updates import UpdateLock, UpdateOptions, atomic_json, page_is_current, plan_documents, run_update, source_snapshot


def retire_other_guides(store, role, selected):
    """Keep immutable history; withdraw alternatives only after selected source is current."""
    jobs = [j for j in plan_documents(store)["pages"] if j["source_id"] == selected]
    if not jobs or not all(page_is_current(store, j) for j in jobs):
        raise WikiError("最新攻略尚未全部审核发布，不能撤下旧攻略")
    retired = []
    for sid in source_snapshot(store):
        if sid.startswith("mcguide/" + role + "/") and sid != selected:
            store.delete_source(sid, game_version=DOCUMENT_SCOPE)
            retired.append(sid)
    return retired


def full_plan(store, knowledge_db, snapshot):
    report = coverage(store, knowledge_db, snapshot)
    known = {r["role_id"]: r for r in report["characters"]}
    roles, errors = [], []
    for row in catalog_rows(snapshot["payload"]):
        item = known.get(row["role_id"])
        if not item or item["name"] != row["name"]:
            errors.append({"role_id": row["role_id"], "name": row["name"], "error": "local_identity_not_verified"})
        else:
            roles.append({**row, "status": "pending"})
    return {"roles": roles, "errors": errors, "coverage_before": report}


def sync_latest(store, knowledge_db, snapshot, *, root, cache_directory, resume=None, progress=None, interval=5):
    root = Path(root).resolve()
    if resume and not re.fullmatch(r"[0-9a-f]{32}", resume):
        raise WikiError("恢复任务须为32位run_id")
    run_id = resume or uuid.uuid4().hex
    target = root / run_id / "report.json"
    # A separate OS lock coordinates the whole roster; per-Wiki locks guard each batch.
    with UpdateLock(store.path.with_name(store.path.name + ".latest-roster")):
        if resume:
            report = json.loads(target.read_text(encoding="utf-8"))
            if report.get("database") != str(store.path.resolve()) or report.get("run_id") != run_id:
                raise WikiError("全角色任务与数据库不匹配")
            snapshot = report["catalog_snapshot"]
            cache_directory = report["cache_directory"]
        else:
            report = {"schema": 1, "run_id": run_id, "database": str(store.path.resolve()),
                      "knowledge_db": str(Path(knowledge_db).resolve()), "cache_directory": str(Path(cache_directory).resolve()),
                      "catalog_snapshot": snapshot, "started_at": utcnow(), **full_plan(store, knowledge_db, snapshot)}
        report["state"] = "running"
        def save():
            report["updated_at"] = utcnow()
            report["summary"] = {state: sum(r["status"] == state for r in report["roles"])
                                 for state in ("completed", "pending", "needs_attention", "retryable_failure", "unavailable")}
            atomic_json(target, report)
        save()
        try:
            for item in report["roles"]:
                if item["status"] in ("completed", "unavailable"):
                    continue
                if progress:
                    progress({"run_id": run_id, "role": item["role_id"], "name": item["name"], "summary": report["summary"]})
                # Persist the batch ID before starting; a crash can resume that exact batch.
                batch_id = item.setdefault("update_run_id", uuid.uuid4().hex)
                batch_path = store.path.parent / "updates" / batch_id / "report.json"
                save()
                options = UpdateOptions(sites=("mcguide",), roles=(item["role_id"],), limit=1, max_pages=100, interval=interval)
                if not batch_path.exists():
                    # Existing coordinator allocates its own ID; intercept the first checkpoint atomically.
                    def checkpoint(event):
                        item["update_run_id"] = event["run_id"]
                        save()
                    result = run_update(store, directory=store.path.parent / "updates", cache_directory=cache_directory,
                                        options=options, progress=checkpoint)
                else:
                    result = run_update(store, directory=store.path.parent / "updates", cache_directory=cache_directory,
                                        resume=batch_id)
                collection = result["collection"]["mcguide"]
                item["collection_status"] = collection["status"]
                item["selections"] = collection.get("selections", [])
                imported = collection["imported"]
                item["errors"] = collection["errors"]
                if imported:
                    selected = imported[-1]["source_id"]
                    item["selected_source"] = selected
                    selected_jobs = [j for j in result["pages"] if j["source_id"] == selected]
                    if collection["status"] == "completed" and selected_jobs and all(page_is_current(store, j) for j in selected_jobs):
                        with UpdateLock(store.path):
                            item["retired_sources"] = sorted(set(item.get("retired_sources", []) + retire_other_guides(store, item["role_id"], selected)))
                        item["status"] = "completed"
                    else:
                        item["status"] = "retryable_failure" if collection["status"] == "retryable_failure" or any(j["status"] == "retryable_failure" for j in selected_jobs) else "needs_attention"
                elif any(s.get("reason") == "no_chinese_guides_in_public_listing" for s in collection["skipped"]):
                    item["status"] = "unavailable"
                else:
                    item["status"] = "retryable_failure" if collection["status"] == "retryable_failure" else "needs_attention"
                save()
            report["state"] = "completed" if not report["errors"] and all(r["status"] == "completed" for r in report["roles"]) else "partial"
        except BaseException:
            report["state"] = "interrupted"
            raise
        finally:
            report["coverage_after"] = coverage(store, knowledge_db, snapshot)
            save()
        return report
