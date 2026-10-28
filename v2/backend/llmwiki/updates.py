"""Checkpointed public-knowledge updates. Never runs from a player chat request."""
from contextlib import closing
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import uuid

from .collector import Collector, CollectionError, DOCUMENT_SCOPE, utcnow
from .compiler import CompileOptions, compile_page, SYSTEM_PROMPT
from .documents import document_parts, PROJECTION, RULES
from .evidence import PROJECTION_VERSION
from .reviewer import review_draft
from .store import WikiError
from . import sources

SCHEMA = 1
SITES = ("mcguide", "bwiki", "official")


def compiler_is_current(page):
    compiler = page.get("compiler", {})
    return (compiler.get("projection_version") == PROJECTION_VERSION + "-" + PROJECTION and
            compiler.get("prompt_sha256") == hashlib.sha256((SYSTEM_PROMPT + RULES).encode()).hexdigest())


@dataclass(frozen=True)
class UpdateOptions:
    sites: tuple = SITES
    roles: tuple = ("1210", "1311", "1408")
    articles: tuple = ()  # Empty means bounded discovery from the official menu.
    limit: int = 5
    interval: float = 5
    max_pages: int = 40

    def __post_init__(self):
        if not self.sites or len(set(self.sites)) != len(self.sites) or any(s not in SITES for s in self.sites):
            raise WikiError("请选择不重复的 mcguide/bwiki/official 来源")
        if len(self.roles) > 10 or any(not re.fullmatch(r"[0-9]{1,12}", str(r)) for r in self.roles):
            raise WikiError("每批最多10个明确的角色ID")
        if len(self.articles) > 10 or any(type(a) is not int or a <= 0 for a in self.articles):
            raise WikiError("公告ID须为正整数，每批最多10项")
        if not 1 <= self.limit <= 10 or not 1 <= self.max_pages <= 100:
            raise WikiError("文档限额须为1–10，编译页数限额须为1–100")
        if not math.isfinite(self.interval) or not 5 <= self.interval <= 60:
            raise WikiError("采集间隔须为5–60秒")
        if self.articles and len(self.articles) > self.limit:
            raise WikiError("明确指定的公告数量不能超过本批文档限额")


class UpdateBusy(WikiError):
    pass


class UpdateLock:
    """OS lock releases on process death; an old lock file never blocks recovery."""
    def __init__(self, db):
        db = Path(db).resolve()
        self.path = db.parent / ("." + db.name + ".update.lock")
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if not self.path.stat().st_size:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            self.file = None
            raise UpdateBusy("该Wiki数据库已有更新任务运行，请待其结束后再恢复") from exc
        return self

    def __exit__(self, *args):
        if self.file:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)


def write_readable_report(path, report):
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")[:400]
    lines = ["# 知识库更新报告", "", f"任务：`{report['run_id']}`", "",
             f"状态：**{report['state']}**；阶段：{report.get('phase', 'starting')}；更新时间：{report.get('updated_at')}", "",
             f"范围：{', '.join(report['options']['sites'])}；采集间隔至少 {report['options']['interval']} 秒。", "",
             "## 采集", "", "| 来源 | 状态 | 本次导入检查数 |", "| --- | --- | ---: |"]
    for site, item in report["collection"].items():
        lines.append(f"| {site} | {item['status']} | {len(item['imported'])} |")
    if report.get("summary"):
        s = report["summary"]
        lines += ["", "## 汇总", "", f"新增来源 {s['new_sources']}；变更来源 {s['changed_sources']}；未变来源 {s['unchanged_sources']}。",
                  "", f"本任务发布/恢复发布 {s['published_pages']} 章；无需重编译 {s['unchanged_pages']} 章；待处理 {s['needs_attention']} 章；暂时失败 {s['retryable_pages']} 章；限额顺延 {s['deferred_pages']} 章。",
                  "", f"HTTP 请求 {s['http_requests']}；缓存命中 {s['cache_hits']}。模型调用与用量详见 report.json 内的 compile/review 记录。"]
    lines += ["", "## 文档", "", "| 文档 | 来源修订 | 状态 |", "| --- | ---: | --- |"]
    for item in report["pages"]:
        lines.append(f"| {cell(item['page_id'])} | {item['source_revision']} | {item['status']} |")
    issues = [f"{site}：{cell(e['message'])}；可恢复={e['retryable']}；冷却至={e.get('retry_at')}"
              for site, item in report["collection"].items() if item["status"] != "completed" for e in item["errors"]]
    issues += [f"{site}：{cell(json.dumps(skipped, ensure_ascii=False))}" for site, item in report["collection"].items()
               if item["status"] != "completed" for skipped in item["skipped"]]
    issues += [f"{p['page_id']}：{cell(p.get('error', {}).get('message', p.get('note', '等待处理')))}"
               for p in report["pages"] if p["status"] not in ("published", "unchanged")]
    issues += [f"{e['source_id']}：{cell(e['message'])}" for e in report.get("planning_errors", [])]
    if report.get("fatal_error"):
        issues.append(cell(report["fatal_error"]["message"]))
    if report.get('impact_reviews'):
        counts=report['impact_reviews']['counts']
        lines += ['', '## 公告影响复核', '', '；'.join(f'{name} {counts[key]}' for key,name in (
            ('needs_review','待核对'),('insufficient_evidence','已核对待补证'),('no_text_change','所列结论无需文本修订'),
            ('revision_required','需要修订'),('revision_published','修订已审核发布')))+'。', '', '复核队列独立于文档编译是否完成；无需文本修改不代表整篇已验证适用新版。']
    lines += ["", "## 待办", "", *(["- " + issue for issue in issues] or ["本批采集、编译与发布无未完成项；公告复核待补证情况另见复核队列。"]), "",
              "恢复本任务（成功步骤复用，限流冷却仍然有效，审核驳回不会自动重生成）：", "", "```powershell",
              f".venv/Scripts/python.exe v2/scripts/update-knowledge.py --live --resume {report['run_id']}", "```", "",
              "执行前备份：本报告目录下的 before.db。原始抓取缓存和历史修订保留。"]
    temporary = path.with_suffix(".md.tmp")
    with temporary.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporary, path)


def error_info(error):
    """Only bounded error classes/statuses; no credentials, headers or raw pages."""
    message = str(error) if isinstance(error, (WikiError, CollectionError)) else type(error).__name__
    retryable = bool(getattr(error, "retryable", False)) if isinstance(error, (WikiError, CollectionError)) else True
    if "编译API失败" in message:
        retryable = bool(re.search(r"Timeout|Connection|HTTP (?:408|429|5\d\d)", message))
    return {"message": message[:1500], "error_type": type(error).__name__, "name": getattr(error, "name", None), "retryable": retryable,
            "host": getattr(error, "host", None), "http_status": getattr(error, "status", None),
            "retry_at": getattr(error, "retry_at", None)}


def source_snapshot(store):
    with store.connection() as c:
        return {r["source_id"]: {"revision": r["revision"], "content_hash": r["content_hash"],
                                 "fingerprint": r["fingerprint"]} for r in c.execute(
            "SELECT r.* FROM sources s JOIN source_revisions r USING(source_id,game_version,locale) "
            "WHERE s.game_version=? AND s.locale='zh' AND s.current_revision=r.revision AND r.deleted=0 "
            "AND json_extract(r.metadata,'$.adapter')='dated_public_document_v1'", (DOCUMENT_SCOPE,))}


def page_is_current(store, job):
    with store.connection() as c:
        row = c.execute("SELECT p.dirty,r.payload,r.fingerprint FROM pages p JOIN page_revisions r "
                        "USING(page_id,game_version,locale) WHERE p.page_id=? AND p.game_version=? AND p.locale='zh' "
                        "AND r.revision=p.active_revision", (job["page_id"], DOCUMENT_SCOPE)).fetchone()
        if row is None or row["dirty"]:
            return False
        page = json.loads(row["payload"])
        if page.get("sources") != [{"source_id": job["source_id"], "revision": job["source_revision"]}]:
            return False
        if not compiler_is_current(page):
            return False
        return bool(c.execute(
            "SELECT 1 FROM drafts d JOIN reviews v ON v.draft_id=d.draft_id AND v.fingerprint=d.fingerprint "
            "WHERE d.page_id=? AND d.game_version=? AND d.locale='zh' AND d.fingerprint=? AND d.state='published' "
            "AND v.review_id=(SELECT MAX(review_id) FROM reviews WHERE draft_id=d.draft_id) AND v.decision='approved'",
            (job["page_id"], DOCUMENT_SCOPE, row["fingerprint"])).fetchone())


def plan_documents(store):
    jobs, errors = [], []
    for sid in sorted(source_snapshot(store)):
        try:
            source = store.get_source(sid, game_version=DOCUMENT_SCOPE)
            data, parts = document_parts(source)
            for index, entries in enumerate(parts):
                job = {"source_id": sid, "source_revision": source["revision"], "part": index, "parts": len(parts),
                       "page_id": "document/" + sid + f"/part-{index + 1}",
                       "title": data["title"] + (f" · 第{index + 1}/{len(parts)}章" if len(parts) > 1 else ""),
                       "text_chars": sum(len(e["text"]) for e in entries)}
                job["status"] = "unchanged" if page_is_current(store, job) else "pending"
                jobs.append(job)
        except (WikiError, ValueError, TypeError, KeyError) as exc:
            errors.append({"source_id": sid, **error_info(exc)})
    from .impact_reviews import queue as impact_queue
    return {"pages": jobs, "errors": errors,'impact_reviews':impact_queue(store)}


def draft_state(store, draft_id, job):
    with store.connection() as c:
        row = c.execute("SELECT payload FROM drafts WHERE draft_id=? AND page_id=? AND game_version=? AND locale='zh'",
                        (draft_id, job["page_id"], DOCUMENT_SCOPE)).fetchone()
        if not row:
            return None
        page = json.loads(row[0])
        if not compiler_is_current(page) or page.get("sources") != [{"source_id": job["source_id"], "revision": job["source_revision"]}]:
            return None
        review = c.execute("SELECT decision FROM reviews WHERE draft_id=? ORDER BY review_id DESC LIMIT 1", (draft_id,)).fetchone()
        return review[0] if review else "pending"


def latest_draft(store, job):
    with store.connection() as c:
        row = c.execute("SELECT draft_id FROM drafts WHERE page_id=? AND game_version=? AND locale='zh' ORDER BY rowid DESC LIMIT 1",
                        (job["page_id"], DOCUMENT_SCOPE)).fetchone()
    if row:
        state = draft_state(store, row[0], job)
        if state:
            return row[0], state
    return None, None


def build_page(store, job, directory, checkpoint, *, compile_fn=None, review_fn=None):
    compile_fn, review_fn = compile_fn or compile_page, review_fn or review_draft
    sid, revision = job["source_id"], job["source_revision"]
    source = store.get_source(sid, game_version=DOCUMENT_SCOPE)
    if source["revision"] != revision:
        raise WikiError("来源已更新，请恢复任务后按新修订重新规划")
    # Recover a completed approval after a crash, without paying for another review.
    draft_id, state = latest_draft(store, job)
    if draft_id:
        job["draft_id"] = draft_id
    if state == "approved":
        job["publication"] = store.publish(draft_id)
        job["status"] = "published"
        checkpoint()
        return
    if state == "rejected":
        job.update(status="needs_attention", note="审核已驳回，未自动重新生成或发布")
        checkpoint()
        return
    if state != "pending":
        job["status"] = "compiling"
        checkpoint()
        compiled = compile_fn(store, job["page_id"], game_version=DOCUMENT_SCOPE, kind="reference", entities=["document:" + sid], topics=[],
                              title=job["title"], document_part=job["part"], options=CompileOptions(max_tokens=12288, total_output_tokens=24576),
                              runs_dir=directory / "compile")
        job["compile"] = compiled
        if compiled.get("published") and page_is_current(store, job):
            job["status"] = "unchanged"
            checkpoint()
            return
        if not compiled.get("valid") or not compiled.get("draft_id"):
            error = error_info(WikiError("; ".join(compiled.get("errors") or ["编译未产生有效草稿"])))
            job.update(status="retryable_failure" if error["retryable"] else "needs_attention", error=error)
            checkpoint()
            return
        draft_id = job["draft_id"] = compiled["draft_id"]
    job["status"] = "reviewing"
    checkpoint()
    reviewed = review_fn(store, draft_id, run_dir=directory / "review" / uuid.uuid4().hex)
    job["review"] = reviewed
    job["draft_id"] = reviewed.get("draft_id", draft_id)
    if reviewed.get("status") != "approved":
        error = error_info(WikiError(reviewed.get("error", "语义审核发现待处理问题")))
        job.update(status="retryable_failure" if error["retryable"] else "needs_attention", error=error)
    else:
        job["publication"] = store.publish(job["draft_id"])
        job["status"] = "published"
    checkpoint()


def _summary(report, after):
    before = report["sources_before"]
    pages = report["pages"]
    return {"new_sources": len(after.keys() - before.keys()),
            "changed_sources": sum(sid in before and before[sid] != value for sid, value in after.items()),
            "unchanged_sources": sum(before.get(sid) == value for sid, value in after.items()),
            "published_pages": sum(p["status"] in ("published", "unchanged") and bool(p.get("publication")) for p in pages),
            "unchanged_pages": sum(p["status"] == "unchanged" and not p.get("publication") for p in pages),
            "needs_attention": sum(p["status"] == "needs_attention" for p in pages),
            "retryable_pages": sum(p["status"] == "retryable_failure" for p in pages),
            "deferred_pages": sum(p["status"] == "deferred" for p in pages),
            "collection_failures": sum(s["status"] != "completed" for s in report["collection"].values()),
            "planning_failures": len(report.get("planning_errors", [])),
            "http_requests": sum(not r.get("cache", False) for r in report["requests"]),
            "cache_hits": sum(bool(r.get("cache")) for r in report["requests"])}


def run_update(store, *, directory, cache_directory, options=None, resume=None, collect=True,
               client_factory=None, collect_fn=None, compile_fn=None, review_fn=None, progress=None):
    """One live batch; retries require another explicit call, never a background loop."""
    root, cache = Path(directory).resolve(), Path(cache_directory).resolve()
    if resume and not re.fullmatch(r"[0-9a-f]{32}", resume):
        raise WikiError("恢复任务须使用报告中的32位run_id")
    run_id = resume or uuid.uuid4().hex
    folder = root / run_id
    target = folder / "report.json"
    with UpdateLock(store.path):
        if resume:
            report = json.loads(target.read_text(encoding="utf-8"))
            if report.get("schema") != SCHEMA or report.get("database") != str(store.path.resolve()) or report.get("run_id") != run_id:
                raise WikiError("更新报告版本或数据库不匹配")
            options = UpdateOptions(**report["options"])
            cache = Path(report["cache_directory"])
            collect = report["collect"]
        else:
            options = options or UpdateOptions()
            report = {"schema": SCHEMA, "run_id": run_id, "database": str(store.path.resolve()),
                      "options": asdict(options), "cache_directory": str(cache), "collect": collect,
                      "started_at": utcnow(), "sources_before": source_snapshot(store),
                      "collection": {}, "pages": [], "requests": [], "attempts": []}
        report.update(state="running", phase="starting", active_site=None, active_page=None)
        report.pop("fatal_error", None)
        report["attempts"].append({"started_at": utcnow()})
        def save():
            report["updated_at"] = utcnow()
            atomic_json(target, report)
            write_readable_report(target.with_suffix(".md"), report)
        def checkpoint():
            save()
            if progress:
                page = next((p for p in report["pages"] if p["page_id"] == report.get("active_page")), {})
                progress({"run_id": run_id, "phase": report.get("phase", "starting"),
                          "site": report.get("active_site"), "page_id": report.get("active_page"),
                          "page_status": page.get("status"), "report": str(target)})
        checkpoint()
        if not (folder / "before.db").exists():
            with closing(sqlite3.connect(store.path)) as original, closing(sqlite3.connect(folder / "before.db")) as backup:
                original.backup(backup)
        try:
            client = (client_factory or Collector)(cache, interval=options.interval)
            client.log = report["requests"]
            if collect:
                for site in options.sites:
                    previous = report["collection"].get(site, {})
                    if previous.get("status") in ("completed", "needs_attention"):
                        continue
                    item = {"status": "running", "started_at": utcnow(), "imported": [], "skipped": [], "errors": [],
                            "previous_attempts": previous.get("previous_attempts", []) + ([previous] if previous else [])}
                    # Do not recursively nest the prior attempt's own history.
                    for attempt in item["previous_attempts"]:
                        attempt.pop("previous_attempts", None)
                    report["collection"][site] = item
                    report.update(phase="collection", active_site=site, active_page=None)
                    checkpoint()
                    try:
                        if collect_fn:
                            collect_fn(site, client, store, options, item, lambda _event: checkpoint())
                        elif site == "mcguide":
                            sources.collect_mcguide(client, store, options.roles, 1, item, progress=lambda _event: checkpoint())
                        elif site == "official":
                            sources.collect_official(client, store, options.limit, item, options.articles or None, progress=lambda _event: checkpoint())
                        else:
                            sources.collect_bwiki(client, store, options.limit, item, progress=lambda _event: checkpoint())
                        item["status"] = "needs_attention" if item["skipped"] else "completed"
                    except Exception as exc:
                        error = error_info(exc)
                        item["errors"].append(error)
                        item["status"] = "retryable_failure" if error["retryable"] else "needs_attention"
                    item["finished_at"] = utcnow()
                    checkpoint()
            plan = plan_documents(store)
            report.update(phase="build", active_site=None, active_page=None)
            report["planning_errors"] = plan["errors"]
            previous = {p["page_id"]: p for p in report["pages"]}
            expected = {p["page_id"] for p in plan["pages"]}
            with store.connection(write=True) as c:
                obsolete = [r[0] for r in c.execute("SELECT page_id FROM pages WHERE game_version=? AND locale='zh' AND page_id LIKE 'document/%'", (DOCUMENT_SCOPE,)) if r[0] not in expected]
                pending = [p["page_id"] for p in plan["pages"] if p["status"] != "unchanged"]
                if obsolete or pending:
                    store._dirty(c, obsolete + pending, (DOCUMENT_SCOPE, "zh"), "document_update_pending", store._bump(c))
            report["obsolete_pages"] = obsolete
            report["pages"] = [{**previous.get(p["page_id"], {}), **p} if previous.get(p["page_id"], {}).get("source_revision") == p["source_revision"] else p for p in plan["pages"]]
            checkpoint()
            remaining = options.max_pages
            for item in report["pages"]:
                report["active_page"] = item["page_id"]
                old = previous.get(item["page_id"], {})
                if item["status"] == "unchanged":
                    if old.get("status") in ("needs_attention", "retryable_failure", "reviewing", "compiling") and not item.get("publication"):
                        current = store.get_page(item["page_id"], game_version=DOCUMENT_SCOPE)
                        item["publication"] = {"revision": current["revision"], "recovered_from_approved_page": True}
                        item["resolution"] = "已找到当前来源对应的有效已审核发布页"
                    continue
                if old.get("source_revision") == item["source_revision"] and old.get("status") == "needs_attention":
                    if latest_draft(store, item)[1] == "rejected":
                        item["status"] = "needs_attention"
                        continue
                if remaining <= 0:
                    item["status"] = "deferred"
                    continue
                remaining -= 1
                try:
                    build_page(store, item, folder, checkpoint, compile_fn=compile_fn, review_fn=review_fn)
                except Exception as exc:
                    error = error_info(exc)
                    item.update(status="retryable_failure" if error["retryable"] else "needs_attention", error=error)
                checkpoint()
            report["sources_after"] = source_snapshot(store)
            from .impact_reviews import queue as impact_queue
            report['impact_reviews']=impact_queue(store)
            report["summary"] = _summary(report, report["sources_after"])
            unsuccessful = plan["errors"] or any(s["status"] != "completed" for s in report["collection"].values()) or any(
                p["status"] not in ("published", "unchanged") for p in report["pages"])
            report["state"] = "partial" if unsuccessful else "completed"
            report.update(phase="finished", active_page=None)
        except BaseException as exc:
            report["state"] = "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit)) else "failed"
            report["fatal_error"] = report["attempts"][-1]["error"] = error_info(exc)
            raise
        finally:
            report["attempts"][-1]["finished_at"] = utcnow()
            report["finished_at"] = utcnow()
            checkpoint()
    return report
