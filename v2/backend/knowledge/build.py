"""镜像文件 -> SQLite 知识库。

这一步**完全离线**:只读 data/raw/ 下的文件,不联网。所以它可以反复跑、
可以在 CI 里跑、可以在改了解析规则之后重跑而不用再去抓一遍。

知识库是可重建的资产(不含用户数据),所以默认 reset=True 整库重来。
这也是为什么它和用户域的表不在一个库里 —— 两者的"能不能丢"完全不同。
"""
import hashlib
import json
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field

from . import audit, config, db, equipment, nanoka, normalize


@dataclass
class BuildReport:
    version: str = ""
    locale: str = ""
    counts: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    capability: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    fields: dict = field(default_factory=dict)

    def summary(self) -> str:
        top = ", ".join(f"{k}={v}" for k, v in sorted(self.counts.items()) if v)
        return f"version={self.version} locale={self.locale} | {top}"


def read_raw(version: str, rel_path: str):
    path = nanoka.local_path(version, rel_path)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def build(
    *,
    version: str | None = None,
    locale: str = config.DEFAULT_LOCALE,
    db_path: str = config.DB_PATH,
    reset: bool = True,
) -> BuildReport:
    report = BuildReport(locale=locale)
    manifest = nanoka.load_manifest(version) if version else _latest_manifest()
    version = version or manifest.get("game_version", "")
    if not version:
        raise RuntimeError(
            "找不到任何已镜像的数据。先跑 scripts/mirror-nanoka.py,或看根目录 README.md 的数据准备与知识维护说明。"
        )
    report.version = version

    manifest = nanoka.load_manifest(version)
    if not manifest.get("files"):
        raise RuntimeError(f"{nanoka.manifest_path(version)} 里没有任何文件记录,先重新镜像。")

    norm = normalize.Normalized()
    _collect(version, locale, manifest, norm, report)

    # 先在独立临时库构建,成功后通过 SQLite backup 事务发布。
    # 不替换 WAL 正在使用的文件,也不在构建失败时破坏上一个可用库。
    with tempfile.TemporaryDirectory(prefix="knowledge-build-") as scratch:
        conn = db.connect(os.path.join(scratch, "knowledge.db"))
        try:
            if not reset and os.path.isfile(db_path):
                old = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True)
                try:
                    old.backup(conn)
                finally:
                    old.close()
                if (db.get_meta(conn, "game_version") != version
                        or db.get_meta(conn, "locale") != locale):
                    raise RuntimeError("--keep 仅支持相同版本和语言;跨版本/语言请整库重建。")
            report.capability = db.init_schema(conn, reset=reset)
            _insert_all(conn, norm.tables, report)
            _build_fts(conn, report)
            db.set_meta(conn, "game_version", version)
            db.set_meta(conn, "locale", locale)
            from . import relations
            digest = relations.rebuild(conn, version, report.notes)
            report.counts["entity_relations"] = conn.execute("SELECT count(*) FROM entity_relations").fetchone()[0]
            db.set_meta(conn, "relations_curation_sha256", digest)
            db.set_meta(conn, "schema_version", "4")
            db.set_meta(conn, "field_audit", json.dumps(report.fields, ensure_ascii=False))
            db.set_meta(conn, "build_notes", json.dumps(norm.notes, ensure_ascii=False))
            db.set_meta(conn, "built_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
            conn.commit()
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("知识库完整性检查失败,未发布")
            target = db.connect(db_path)
            try:
                conn.backup(target)
            finally:
                target.close()
        finally:
            conn.close()

    report.notes = [*report.notes, *norm.notes]
    report.skipped = norm.skipped
    return report


def _latest_manifest() -> dict:
    """没有指定版本时,取 raw/ 下最新的一个版本目录(按名字排序,版本号可比）。"""
    if not os.path.isdir(config.RAW_DIR):
        return {}
    versions = [
        d for d in os.listdir(config.RAW_DIR)
        if os.path.isfile(os.path.join(config.RAW_DIR, d, "_manifest.json"))
    ]
    if not versions:
        return {}

    def key(v: str):
        return [int(x) if x.isdigit() else 0 for x in v.split(".")]

    return nanoka.load_manifest(sorted(versions, key=key)[-1])


# --------------------------------------------------------------------------- 装配


def _store_raw(version: str, locale: str, files: dict, norm) -> None:
    """把原文原样存进 raw_docs。

    这不是冗余备份,它是"可重放"的前提:归一化规则写错、表结构改了、
    解析器升级了 —— 只要 raw_docs 在,就能只重建上层而不用再联网一次。
    代价是数据库会大一倍(12MB 原文 + 索引),这个代价可以接受。

    locale 的处理:版本根目录下的文件是**语言无关**的(内部内联了 zh/en/ja/ko),
    存 locale='' 以免同一份内容被不同语言各存一遍。
    """
    locale_scoped = {"item", "hyperlink", "character", "weapon_detail", "echo_detail"}
    for rel, meta in sorted(files.items()):
        path = nanoka.local_path(version, rel)
        if not os.path.isfile(path):
            raise RuntimeError(f"清单里的原文缺失: {path};未发布知识库")
        with open(path, "rb") as fh:
            raw_bytes = fh.read()
        payload = raw_bytes.decode("utf-8")
        actual_hash = hashlib.sha256(raw_bytes).hexdigest()
        if meta.get("sha256") and meta["sha256"] != actual_hash:
            raise RuntimeError(f"镜像校验失败: {path};请重新镜像")
        kind = meta.get("kind", "")
        norm.add(
            "raw_docs",
            {
                "kind": kind,
                "doc_id": meta.get("doc_id") or rel,
                "game_version": version,
                "locale": locale if kind in locale_scoped else "",
                "payload": payload,
                "sha256": actual_hash,
                "source_url": meta.get("url", ""),
                "fetched_at": meta.get("fetched_at", ""),
            },
        )


def _collect(version: str, locale: str, manifest: dict, norm, report: BuildReport) -> None:
    files = {rel: meta for rel, meta in manifest.get("files", {}).items()
             if "/" not in rel or rel.startswith(locale + "/")}
    _store_raw(version, locale, files, norm)
    for rel, meta in sorted(files.items()):
        payload = read_raw(version, rel)
        if not isinstance(payload, dict):
            raise RuntimeError(f"源文件不是对象: {rel}")
        report.fields[rel] = audit.inspect(meta.get("kind", ""), payload)
        for section, stats in [("root", report.fields[rel]), *[
            (k, v) for k, v in report.fields[rel].items() if isinstance(v, dict) and "unknown_fields" in v
        ]]:
            if stats["unknown_fields"]:
                report.warnings.append(f"{rel} {section} 新字段仅保留在原文: {stats['unknown_fields']}")
    roster = read_raw(version, "character.json") or {}
    if not roster:
        report.warnings.append("character.json 缺失或为空 —— 角色相关的表会是空的")

    # 角色详情:按 manifest 里记录过的 character 类文件读取。
    details: dict[str, dict] = {}
    detail_files = [
        rel for rel, meta in files.items()
        if meta.get("kind") == "character"
    ]
    for rel in sorted(detail_files):
        cid = files[rel].get("doc_id") or os.path.basename(rel).split(".")[0]
        payload = read_raw(version, rel)
        if payload:
            details[str(cid)] = payload
    if roster and len(details) < len(roster):
        report.warnings.append(
            f"角色详情只拿到 {len(details)}/{len(roster)} —— "
            "如果是 --limit 跑的镜像,这是预期内的;否则重跑镜像补齐。"
        )

    normalize.normalize_characters(roster, details, version, norm)

    for rel, meta in sorted(files.items()):
        kind = meta.get("kind")
        # roster 与 character 详情已经在上面处理过;这里只处理其余实体。
        if kind in ("roster", "character"):
            continue
        payload = read_raw(version, rel)
        if payload is None:
            report.warnings.append(f"清单里有 {rel} 但本地文件不存在,已跳过")
            continue
        if kind == "weapon":
            normalize.normalize_weapons(payload, version, norm)
        elif kind == "echo":
            normalize.normalize_echoes(payload, version, norm)
        elif kind == "sonata":
            normalize.normalize_sonatas(payload, version, locale, norm)
        elif kind == "monster":
            normalize.normalize_monsters(payload, version, norm)
        elif kind == "item":
            normalize.normalize_items(payload, version, norm)
        elif kind == "hyperlink":
            normalize.normalize_hyperlinks(payload, norm)
        elif kind == "weapon_detail":
            equipment.normalize_weapon(payload, norm)
        elif kind == "echo_detail":
            equipment.normalize_echo(payload, norm)

    for kind in ("weapon", "echo"):
        index = read_raw(version, f"{kind}.json") or {}
        found = {str(meta.get("doc_id")) for meta in files.values() if meta.get("kind") == kind + "_detail"}
        if index and len(found) < len(index):
            report.warnings.append(f"{kind} 详情覆盖 {len(found)}/{len(index)};未镜像的条目仍只有索引数据")


def _insert_all(conn, tables: dict[str, list[dict]], report: BuildReport) -> None:
    for table, rows in tables.items():
        if not rows:
            continue
        # 列取所有行的并集并补齐 —— 源数据字段偶尔缺项,按第一行定列会丢数据。
        cols: list[str] = []
        for row in rows:
            for c in row:
                if c not in cols:
                    cols.append(c)
        placeholders = ", ".join("?" for _ in cols)
        col_sql = ", ".join(f'"{c}"' for c in cols)
        stmt = f'INSERT OR REPLACE INTO "{table}" ({col_sql}) VALUES ({placeholders})'
        payload = [tuple(row.get(c) for c in cols) for row in rows]
        try:
            conn.executemany(stmt, payload)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"写入 {table} 失败,未发布知识库: {exc}") from exc
        # 报**实际行数**而不是尝试写入的条数:主键去重(例如 refs 有大量重复边、
        # aliases 有跨角色重名)会让两者差很多,报尝试数会误导。
        actual = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        report.counts[table] = actual
        if actual != len(payload):
            report.notes.append(f"{table}: 尝试写入 {len(payload)} 条,去重后 {actual} 条")
    conn.commit()


def _build_fts(conn, report: BuildReport) -> None:
    if not report.capability.get("trigram"):
        report.warnings.append(
            "本机 FTS5/trigram 不可用,全文检索这一层没有建。"
            "其余功能不受影响;检索会退回 LIKE。"
        )
        return
    conn.execute("DELETE FROM fts_text")
    conn.execute(
        "INSERT INTO fts_text(entity_type, entity_id, field, body) "
        "SELECT entity_type, entity_id, field, body FROM text_index"
    )
    conn.commit()
    report.counts["fts_text"] = conn.execute("SELECT count(*) FROM fts_text").fetchone()[0]
