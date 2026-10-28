"""Rebuildable entity edges; recommendations never imply signature ownership."""
import hashlib
import json
from pathlib import Path
import sqlite3

TABLES = {"character": "characters", "weapon": "weapons", "echo": "echoes",
          "sonata": "sonatas", "monster": "monsters", "item": "items", "hyperlink": "hyperlinks"}
RELATIONS = ("recommended_weapon", "signature_weapon", "sonata", "echo", "ascension_material")
CURATED_PATH = Path(__file__).with_name("curated_relations.json")


def rebuild(conn: sqlite3.Connection, version: str, notes: list[str]) -> str:
    conn.execute("DELETE FROM entity_relations")
    def add(ft, fid, relation, tt, tid, field, url, kind="source_field"):
        conn.execute("INSERT OR IGNORE INTO entity_relations VALUES (?,?,?,?,?,?,?,?,?)",
                     (ft, str(fid), relation, tt, str(tid), field, url, kind, version))
    def source(kind, doc_id=None):
        sql, params = "SELECT source_url FROM raw_docs WHERE kind=?", [kind]
        if doc_id is not None:
            sql += " AND doc_id=?"
            params.append(str(doc_id))
        row = conn.execute(sql + " LIMIT 1", params).fetchone()
        return row[0] if row else None
    for r in conn.execute("SELECT character_id, ref_id, idx FROM character_recommend WHERE kind='weapon'"):
        add("character", r[0], "recommended_weapon", "weapon", r[1], f"recommend.weapon[{r[2]}]", source("character", r[0]))
    for r in conn.execute("SELECT echo_id, sonata_id FROM echo_sonatas"):
        add("echo", r[0], "sonata", "sonata", r[1], "group", source("echo_detail", r[0]))
    for r in conn.execute("SELECT id, echo_id FROM monsters WHERE echo_id>0"):
        add("monster", r[0], "echo", "echo", r[1], "echo_id", source("monster"))
    for table, owner, etype, raw_kind in (("character_ascensions", "character_id", "character", "character"),
                                          ("weapon_ascensions", "weapon_id", "weapon", "weapon_detail")):
        for r in conn.execute(f"SELECT DISTINCT {owner}, item_key FROM {table}"):
            add(etype, r[0], "ascension_material", "item", r[1], "ascensions", source(raw_kind, r[0]))
    payload = CURATED_PATH.read_bytes()
    for row in json.loads(payload):
        if version not in row["versions"]:
            continue
        ft, tt = row["from_type"], row["to_type"]
        if ft not in TABLES or tt not in TABLES or row["relation"] not in RELATIONS:
            raise ValueError("人工关联包含未支持的类型")
        endpoints = [conn.execute(f"SELECT name FROM {TABLES[t]} WHERE id=?", (row[k + "_id"],)).fetchone()
                     for t, k in ((ft, "from"), (tt, "to"))]
        if not all(endpoints):
            notes.append(f"跳过当前镜像中缺少端点的人工关联: {row['from_id']}->{row['to_id']}")
            continue
        if [r[0] for r in endpoints] != [row["from_name"], row["to_name"]]:
            raise ValueError("人工关联ID与当前实体名称不一致，未发布")
        if row["relation"] == "signature_weapon" and not conn.execute(
                "SELECT 1 FROM character_recommend WHERE character_id=? AND kind='weapon' AND ref_id=?",
                (row["from_id"], row["to_id"])).fetchone():
            raise ValueError("专武人工关联未通过推荐实体ID交叉核对，需重新审核")
        add(ft, row["from_id"], row["relation"], tt, row["to_id"], row["source_field"],
            row["source_url"], row["evidence_kind"])
    return hashlib.sha256(payload).hexdigest()


def resolve(conn, entity_type, name):
    """Exact alias/name/ID resolution with explicit ambiguity, never a guessed first row."""
    if entity_type not in TABLES:
        return {"error": "不支持的实体类型"}
    key = str(name or "").strip()
    if not key:
        return {"error": "实体名称不能为空"}
    rows = conn.execute(f"SELECT id,name FROM {TABLES[entity_type]} WHERE CAST(id AS TEXT)=? ORDER BY id", (key,)).fetchall()
    if not rows:
        rows = conn.execute(f"SELECT id,name FROM {TABLES[entity_type]} WHERE lower(name)=lower(?) ORDER BY id", (key,)).fetchall()
    if not rows:
        rows = conn.execute(
            f"SELECT DISTINCT e.id, e.name FROM {TABLES[entity_type]} e JOIN aliases a "
            "ON a.entity_type=? AND a.entity_id=CAST(e.id AS TEXT) "
            "WHERE lower(a.alias)=lower(?) ORDER BY e.id", (entity_type, key)).fetchall()
    if len(rows) != 1:
        candidates = [dict(r) for r in rows[:10]]
        if entity_type == "character" and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='character_profiles'").fetchone():
            for candidate in candidates:
                profile = conn.execute("SELECT sex FROM character_profiles WHERE character_id=?", (candidate["id"],)).fetchone()
                if profile and profile[0]:
                    candidate["sex"] = profile[0]
        return {"error": "实体不存在或名称有歧义，请使用完整名称或ID", "total_candidates": len(rows),
                "candidates": candidates}
    return dict(rows[0])


def query(conn, entity_type, name, relation=None, direction="outgoing", limit=5, offset=0):
    if relation is not None and relation not in RELATIONS:
        return {"error": "不支持的关系类型", "supported": list(RELATIONS)}
    if direction not in ("outgoing", "incoming"):
        return {"error": "direction 必须为 outgoing/incoming"}
    limit, offset = max(1, min(int(limit), 20)), int(offset)
    if offset < 0:
        return {"error": "offset 不能为负数"}
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='entity_relations'").fetchone():
        return {"error": "关联层尚未构建，请离线重建知识库"}
    entity = resolve(conn, entity_type, name)
    if "error" in entity:
        key = str(name or "").strip()
        if (entity_type in TABLES and key.isdecimal() and not entity.get("total_candidates")
                and conn.execute("SELECT 1 FROM entity_relations WHERE (from_type=? AND from_id=?) "
                                 "OR (to_type=? AND to_id=?) LIMIT 1", (entity_type, key, entity_type, key)).fetchone()):
            entity = {"id": key, "name": None, "resolved": False}
        else:
            return entity
    prefix = "from" if direction == "outgoing" else "to"
    opposite = "to" if direction == "outgoing" else "from"
    where, args = f"{prefix}_type=? AND {prefix}_id=?", [entity_type, str(entity["id"])]
    if relation:
        where += " AND relation=?"
        args.append(relation)
    total = conn.execute("SELECT count(*) FROM entity_relations WHERE " + where, args).fetchone()[0]
    rows = conn.execute("SELECT * FROM entity_relations WHERE " + where +
                        " ORDER BY relation, source_field, from_type, from_id, to_type, to_id LIMIT ? OFFSET ?",
                        [*args, limit, offset]).fetchall()
    results = []
    for row in rows:
        edge = dict(row)
        target_type, target_id = edge[opposite + "_type"], edge[opposite + "_id"]
        target = conn.execute(f"SELECT name FROM {TABLES[target_type]} WHERE id=?", (target_id,)).fetchone()
        edge["related_entity"] = {"entity_type": target_type, "id": target_id,
                                  "name": target[0] if target else None, "resolved": bool(target and target[0])}
        results.append(edge)
    return {"entity_type": entity_type, "entity": entity, "direction": direction,
            "relations": results, "total": total, "offset": offset,
            "next_offset": offset + len(rows) if offset + len(rows) < total else None,
            "note": "recommended_weapon仅为源站推荐及其原始顺序，不等于专武或强度排名；signature_weapon为单独核对关联。无记录不代表不存在关系。关联不证明配队最优或伤害提升。"}
