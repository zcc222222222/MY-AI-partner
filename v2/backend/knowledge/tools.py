"""知识库工具:可插拔注册表。

刻意做成和 app/agents/tools.py **完全相同的形状**
(TOOLS / HANDLERS / execute_tool),所以被后端并入时直接 update 即可,
不需要适配层 —— 编排器、路由、前端也都不用动。

这几个工具走的是"确定性通道":答案由 SQL 直接给出,不经过检索、不经过 LLM。
所以它们不会幻觉 —— 数值就是数值。这也意味着它们**不需要**被塞进向量库。

设计上的一个刻意选择:所有失败都降级成 JSON 里的 error 字段,绝不抛异常。
调用方是 function-calling 循环,一个抛出的异常会变成一次聊天的 500;
而一条 "error: 没找到角色 X" 会被模型读进去并自我纠正。
"""
import json
import re
import sqlite3
import threading
from typing import Any, Callable

from . import config, relations

_local = threading.local()


class KnowledgeBaseMissing(RuntimeError):
    """知识库尚未构建。调用方应当给出可操作的提示,而不是一个 traceback。"""


def connect(db_path: str | None = None, *, reuse: bool = True) -> sqlite3.Connection:
    """以**只读**方式打开知识库。

    工具层只读是一个安全属性:模型无论怎么调工具都改不了知识库。
    构建是 scripts/build-knowledge-db.py 的事,那条路径不经过这里。
    """
    path = db_path or config.DB_PATH

    if reuse and getattr(_local, "conn", None) is not None and _local.path == path:
        return _local.conn
    if not _is_readable(path):
        raise KnowledgeBaseMissing(
            f"知识库不存在或不可读: {path}\n"
            "先执行:python v2/scripts/mirror-nanoka.py 然后 python v2/scripts/build-knowledge-db.py\n"
            "(见根目录 README.md 的数据准备与知识维护说明)"
        )
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    if reuse:
        close()
        _local.conn, _local.path = conn, path
    return conn


def _is_readable(path: str) -> bool:
    import os

    return os.path.isfile(path) and os.path.getsize(path) > 0


def close() -> None:
    if getattr(_local, "conn", None) is not None:
        _local.conn.close()
    _local.conn, _local.path = None, None


def _to_int(value) -> int | None:
    """把工具参数收敛成整数。模型给的整数参数经常是字符串。"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- 解析实体


def resolve_character(conn: sqlite3.Connection, name: str) -> dict | None:
    """把一个名字解析成角色。

    优先名称、别名或ID精确匹配，只有唯一候选才允许模糊兜底。
    漂泊者的元素/男女记录不能按LIMIT 1随机选取。
    """
    if not name:
        return None
    key = str(name).strip()

    exact = relations.resolve(conn, "character", key)
    if "error" not in exact:
        return dict(conn.execute("SELECT * FROM characters WHERE id=?", (exact["id"],)).fetchone())
    if exact.get("total_candidates"):
        return None
    rows = conn.execute("SELECT DISTINCT c.* FROM characters c LEFT JOIN aliases a ON a.entity_type='character' "
                        "AND a.entity_id=CAST(c.id AS TEXT) WHERE a.alias LIKE ? OR c.name LIKE ? ORDER BY c.id LIMIT 2",
                        (f"%{key}%", f"%{key}%")).fetchall()
    return dict(rows[0]) if len(rows) == 1 else None


def suggest_characters(conn: sqlite3.Connection, name: str, limit: int = 5) -> list[str]:
    """找不到时给几个候选 —— 让模型有机会自我纠正,而不是直接失败。

    一并搜别名表:输入 "Sanh" 这类半截英文名时,只比对 characters.name 是找不到的,
    而那些名字全都躺在 aliases 里。
    """
    key = str(name or "").strip()
    if not key:
        return []
    like = f"%{key}%"
    rows = conn.execute(
        "SELECT DISTINCT c.name, length(c.name) AS n FROM characters c "
        "LEFT JOIN aliases a ON a.entity_type='character' AND a.entity_id = CAST(c.id AS TEXT) "
        "WHERE c.name LIKE ? OR c.nick_name LIKE ? OR a.alias LIKE ? "
        "ORDER BY n LIMIT ?",
        (like, like, like, limit),
    ).fetchall()
    return [r["name"] for r in rows]


# --------------------------------------------------------------------------- 工具实现

def _character_lookup_error(conn, name):
    detail = relations.resolve(conn, "character", name)
    result = {"error": f"角色名称不存在或有歧义「{name}」，请补充形态、性别或ID", "suggestions": suggest_characters(conn, name)}
    if detail.get("total_candidates"):
        result["candidates"] = detail["candidates"]
    return result


def _get_character_stats(name: str, phase: int | None = None) -> dict:
    conn = connect()
    char = resolve_character(conn, name)
    if char is None:
        return _character_lookup_error(conn, name)
    sql = "SELECT phase, stage, life, atk, def FROM character_stats WHERE character_id = ?"
    params: list[Any] = [char["id"]]
    # 模型经常把整数参数写成字符串("0" / "phase 0"),SQLite 的类型亲和性不会报错,
    # 但会静默查不到 —— 那会被模型解读成"这个角色没有数据"。所以在这里显式收敛。
    phase = _to_int(phase)
    if phase is not None:
        sql += " AND phase = ?"
        params.append(phase)
    rows = conn.execute(sql + " ORDER BY phase, stage", params).fetchall()
    if not rows:
        return {
            "error": f"角色「{char['name']}」没有面板数据(源数据里该字段可能为空)",
            "character": char["name"],
        }
    return {
        "character": char["name"],
        "nick_name": char["nick_name"],
        "rarity": char["rarity"],
        "element": char["element_name"] or char["element"],
        "weapon": char["weapon_name"] or char["weapon"],
        "source": f"nanoka {char['game_version']} / character/{char['id']}.json",
        "stats": [
            {
                "phase": r["phase"],
                "stage": r["stage"],
                "life": r["life"],
                "atk": r["atk"],
                "def": r["def"],
            }
            for r in rows
        ],
    }


def _get_character_profile(name: str) -> dict:
    conn = connect()
    char = resolve_character(conn, name)
    if char is None:
        return _character_lookup_error(conn, name)
    profile = conn.execute(
        "SELECT * FROM character_profiles WHERE character_id = ?", (char["id"],)
    ).fetchone()
    tags = conn.execute(
        "SELECT name, desc FROM character_tags WHERE character_id = ?", (char["id"],)
    ).fetchall()
    data = {
        "character": char["name"],
        "nick_name": char["nick_name"],
        "rarity": char["rarity"],
        "element": char["element_name"] or char["element"],
        "weapon": char["weapon_name"] or char["weapon"],
        "tags": [{"name": t["name"], "desc": t["desc"]} for t in tags],
        "source": f"nanoka {char['game_version']} / character/{char['id']}.json",
    }
    if profile:
        p = dict(profile)
        data.update(
            {
                "birthday": p.get("birth"),
                "sex": p.get("sex"),
                "country": p.get("country"),
                "affiliation": p.get("influence"),
                "cv": {
                    "cn": p.get("cv_cn"),
                    "jp": p.get("cv_jp"),
                    "ko": p.get("cv_ko"),
                    "en": p.get("cv_en"),
                },
            }
        )
    data["equipment_links"] = {
        kind: relations.query(conn, "character", str(char["id"]), relation=kind)
        for kind in ("signature_weapon", "recommended_weapon")
    }
    return data


def _get_ascension_materials(name: str, phase: int | None = None) -> dict:
    conn = connect()
    char = resolve_character(conn, name)
    if char is None:
        return _character_lookup_error(conn, name)
    sql = (
        "SELECT a.phase, a.item_key, a.amount, i.name AS item_name "
        "FROM character_ascensions a LEFT JOIN items i ON i.id = a.item_key "
        "WHERE a.character_id = ?"
    )
    params: list[Any] = [char["id"]]
    phase = _to_int(phase)  # 同上:容忍模型传字符串
    if phase is not None:
        sql += " AND a.phase = ?"
        params.append(phase)
    rows = conn.execute(sql + " ORDER BY a.phase, a.item_key", params).fetchall()
    return {
        "character": char["name"],
        "source": f"nanoka {char['game_version']} / character/{char['id']}.json",
        "materials": [
            {
                "phase": r["phase"],
                "item_id": r["item_key"],
                # item_key 可能是货币/经验这类不在 item.json 里的 id,名称留空是诚实的结果
                "item_name": r["item_name"] or "未在 item.json 中找到",
                "amount": r["amount"],
            }
            for r in rows
        ],
        "note": "item_key 是源数据的整数 id;未能 JOIN 到名称的条目见 item_name 字段。",
    }


def _search_knowledge_text(query: str, limit: int = 5) -> dict:
    """全文检索。

    这里体现了实测出来的中文检索约束:trigram 索引只对 >=3 字的查询有效,
    2 字查询(今州、散华、长离)必然落空。所以短查询直接走 LIKE,
    并且**明确告诉调用方走的是哪条路** —— 不让它误以为是"没有相关内容"。
    """
    conn = connect()
    q = (query or "").strip()
    if not q:
        return {"error": "查询为空"}
    limit = max(1, min(int(limit or 5), 20))

    rows: list[sqlite3.Row] = []
    route = ""
    if len(q) >= 3:
        try:
            rows = conn.execute(
                "SELECT entity_type, entity_id, field, body, bm25(fts_text) AS score "
                "FROM fts_text WHERE fts_text MATCH ? ORDER BY score LIMIT ?",
                (q, limit),
            ).fetchall()
            route = "fts5-trigram"
        except sqlite3.OperationalError:
            rows = []
    if not rows:
        rows = conn.execute(
            "SELECT entity_type, entity_id, field, body, 0.0 AS score FROM text_index "
            "WHERE body LIKE ? LIMIT ?",
            (f"%{q}%", limit),
        ).fetchall()
        route = "like-fallback"

    return {
        "query": q,
        "route": route,
        "note": (
            "trigram 索引对 2 字查询无效,已自动退回 LIKE"
            if route == "like-fallback" and len(q) < 3
            else ""
        ),
        "results": [
            {
                "entity_type": r["entity_type"],
                "entity_id": r["entity_id"],
                "field": r["field"],
                "snippet": (r["body"] or "")[:200],
            }
            for r in rows
        ],
    }


# --------------------------------------------------------------------------- 注册表


def _get_character_skills(name: str, tree_id: str | None = None, level: int = 1,
                          chain_no: int | None = None, offset: int = 0) -> dict:
    """倍率按源数组取某一级的值,不计算 format 或伤害公式。"""
    conn = connect()
    char = resolve_character(conn, name)
    if char is None:
        return _character_lookup_error(conn, name)
    level = int(level)
    if not 1 <= level <= 20:
        return {"error": "技能等级必须在 1–20 之间"}
    source = f"nanoka {char['game_version']} / character/{char['id']}.json"
    offset = int(offset)
    if offset < 0:
        return {"error": "offset 不能为负数"}
    if tree_id is not None and chain_no is not None:
        return {"error": "tree_id 与 chain_no 请分开查询"}
    def page(text):
        text = text or ""
        end = min(len(text), offset + 5000)
        return {"desc": text[offset:end], "offset": offset, "total_chars": len(text),
                "complete": offset == 0 and end == len(text),
                "next_offset": end if end < len(text) else None,
                "note": "分页是原文片段，须读完后才能判断完整触发条件。"}
    if chain_no is not None:
        row = conn.execute("SELECT chain_no, name, desc FROM character_chains WHERE character_id=? AND chain_no=?",
                           (char["id"], int(chain_no))).fetchone()
        return {"character": char["name"], "source": source, "skills": [],
                "chains": [{"chain_no": row["chain_no"], "name": row["name"], **page(row["desc"])}] if row else []}
    if tree_id is None:
        return {"character": char["name"], "source": source, "view": "catalog",
                "skills": [dict(r) for r in conn.execute(
                    "SELECT tree_id, name FROM character_skills WHERE character_id=? ORDER BY CAST(tree_id AS INTEGER)", (char["id"],))],
                "chains": [dict(r) for r in conn.execute(
                    "SELECT chain_no, name FROM character_chains WHERE character_id=? ORDER BY chain_no", (char["id"],))],
                "note": "此为目录，不含效果。按 tree_id 查单个技能、chain_no 查单条共鸣链；长描述按 next_offset 继续读。"}
    params = [char["id"]]
    sql = "SELECT * FROM character_skills WHERE character_id=?"
    if tree_id is not None:
        sql += " AND tree_id=?"
        params.append(str(tree_id))
    skills = []
    for row in conn.execute(sql + " ORDER BY CAST(tree_id AS INTEGER)", params):
        skill = dict(row)
        tid = skill["tree_id"]
        details = conn.execute("SELECT * FROM character_skill_details WHERE character_id=? AND tree_id=?",
                               (char["id"], tid)).fetchone()
        skill.pop("character_id", None)
        skill.update(page(skill.get("desc")))
        skill["desc_params"] = json.loads(skill["desc_params"] or "[]")
        skill["consumes"] = json.loads(skill["consumes"] or "null")
        if details:
            skill["type"] = details["skill_type"]
            skill["simple_desc"] = details["simple_desc"]
            skill["parent_nodes"] = json.loads(details["parent_nodes"] or "null")
            skill["branch_ids"] = json.loads(details["branch_ids"] or "null")
            skill["unlock_condition"] = details["unlock_condition"]
        attributes = []
        for attr in conn.execute("SELECT * FROM character_skill_levels WHERE character_id=? AND tree_id=? ORDER BY CAST(attribute_id AS INTEGER)",
                                 (char["id"], tid)):
            arrays = json.loads(attr["params"] or "[]")
            attributes.append({"name": attr["name"], "values": [
                values[level - 1] if isinstance(values, list) and len(values) >= level else None
                for values in arrays
            ], "format": json.loads(attr["format"] or "null")})
        skill["attributes"] = attributes
        skill["upgrade_materials"] = [dict(r) for r in conn.execute(
            "SELECT c.item_key AS item_id, i.name AS item_name, c.amount FROM character_skill_costs c "
            "LEFT JOIN items i ON i.id=c.item_key WHERE c.character_id=? AND c.tree_id=? AND c.level=? ORDER BY c.idx",
            (char["id"], tid, level))]
        skills.append(skill)
    chains = []
    return {"character": char["name"], "level": level, "skills": skills, "chains": chains,
            "source": f"nanoka {char['game_version']} / character/{char['id']}.json",
            "note": "values 为源数组对应等级的各参数,未计算伤害;null 表示该等级缺数据。"}


def _get_knowledge_entity(entity_type: str, name: str, level: int = 90,
                          phase: int | None = None, refinement: int = 1,
                          skill_rank: int = 5) -> dict:
    tables = {"weapon": "weapons", "echo": "echoes", "sonata": "sonatas",
              "monster": "monsters", "item": "items", "hyperlink": "hyperlinks"}
    table = tables.get(entity_type)
    if table is None:
        return {"error": "entity_type 必须为 weapon/echo/sonata/monster/item/hyperlink"}
    conn = connect()
    # Possessive shorthand is a relationship, never a global alias for “专武”.
    possessive = re.fullmatch(r"(.+?)(?:的)?(?:专武|专属武器)", str(name).strip()) if entity_type == "weapon" else None
    if possessive:
        linked = relations.query(conn, "character", possessive.group(1), relation="signature_weapon")
        if "error" in linked:
            return linked
        if linked["total"] != 1 or not linked["relations"][0]["related_entity"]["resolved"]:
            return {"error": "没有唯一、已核对的专武关联；请查询推荐武器或提供完整武器名。",
                    "relation_lookup": linked}
        edge = linked["relations"][0]
        result = _get_knowledge_entity("weapon", edge["to_id"], level, phase, refinement, skill_rank)
        result["resolved_from"] = {"query": name, "relation": "signature_weapon", "evidence": edge}
        return result
    rows = conn.execute(
        f"SELECT DISTINCT e.* FROM {table} e LEFT JOIN aliases a ON a.entity_type=? "
        "AND a.entity_id=CAST(e.id AS TEXT) WHERE e.name=? OR lower(a.alias)=lower(?) OR CAST(e.id AS TEXT)=?",
        (entity_type, name, name, name)).fetchall()
    if len(rows) != 1:
        return {"error": "没有精确匹配或名称有歧义,请使用名称或 id", "candidates": [dict(r) for r in rows]}
    entity = dict(rows[0])
    detail_source = None
    if entity_type == "weapon":
        detail = conn.execute("SELECT * FROM weapon_details WHERE weapon_id=?", (entity["id"],)).fetchone()
        entity["detail_available"] = detail is not None
        if detail:
            level, refinement = int(level), int(refinement)
            if not 1 <= level <= 90 or not 1 <= refinement <= 5:
                return {"error": "武器等级须为 1–90,谐振阶级须为 1–5"}
            entity["desc"] = detail["desc"]
            entity["is_skin"] = bool(detail["is_skin"])
            entity["effect_name"] = detail["effect_name"]
            effect = conn.execute("SELECT desc, params FROM weapon_effects WHERE weapon_id=? AND refinement=?",
                                  (entity["id"], refinement)).fetchone()
            entity["refinement"] = refinement
            entity["effect"] = effect["desc"] if effect else detail["effect_template"]
            if re.search(r"\{\d+\}", entity["effect"] or ""):
                entity["effect_note"] = "此阶级源参数存在缺值,占位符未展开;不能推测或补写该数值。"
            entity["effect_params"] = json.loads(effect["params"]) if effect else None
            stats_sql = "SELECT phase, level, name, raw_value, is_ratio, is_percent FROM weapon_stats WHERE weapon_id=? AND level=?"
            stats_params = [entity["id"], level]
            if phase is not None:
                stats_sql += " AND phase=?"
                stats_params.append(int(phase))
            entity["stats"] = [dict(r) for r in conn.execute(stats_sql + " ORDER BY phase, idx", stats_params)]
            entity["stats_note"] = "raw_value 保留源数值及比例标记,不是直接显示的百分数;同等级的突破前后面板按 phase 分开。"
            if entity["is_skin"]:
                entity["stats_note"] = "此条目是武器外观投影,源数据不提供面板、被动和突破材料。"
            entity["ascensions"] = [dict(r) for r in conn.execute(
                "SELECT a.phase, a.item_key AS item_id, i.name AS item_name, a.amount FROM weapon_ascensions a "
                "LEFT JOIN items i ON i.id=a.item_key WHERE a.weapon_id=? ORDER BY a.phase, a.idx", (entity["id"],))]
            detail_source = "weapon_detail"
    if entity_type == "sonata":
        entity["effects"] = [dict(r) for r in conn.execute(
            "SELECT pieces, desc, params FROM sonata_effects WHERE sonata_id=? ORDER BY pieces", (entity["id"],))]
    if entity_type == "monster":
        echo = conn.execute("SELECT name FROM echoes WHERE id=?", (entity["echo_id"],)).fetchone()
        entity["echo_name"] = echo["name"] if echo else None
    if entity_type == "echo":
        extra = conn.execute("SELECT payload FROM entity_extras WHERE entity_type='echo' AND entity_id=?",
                             (str(entity["id"]),)).fetchone()
        entity.update(json.loads(extra["payload"]) if extra else {})
        detail = conn.execute("SELECT * FROM echo_details WHERE echo_id=?", (entity["id"],)).fetchone()
        entity["detail_available"] = detail is not None
        if detail:
            skill_rank = int(skill_rank)
            if not 1 <= skill_rank <= 5:
                return {"error": "声骸技能档位须为 1–5,不是声骸品质或 COST"}
            for field in ("type", "place", "intensity", "intensity_code", "monster_info", "simple_desc"):
                entity[field] = detail[field]
            if detail["phantom"]:
                entity["phantom"] = json.loads(detail["phantom"])
            skill = conn.execute("SELECT desc, params FROM echo_skills WHERE echo_id=? AND skill_rank=?",
                                 (entity["id"], skill_rank)).fetchone()
            entity["skill_rank"] = skill_rank
            entity["skill"] = skill["desc"] if skill else detail["skill_template"]
            entity["skill_params"] = json.loads(skill["params"]) if skill else None
            entity["sonatas"] = [dict(r) for r in conn.execute(
                "SELECT sonata_id, name FROM echo_sonatas WHERE echo_id=? ORDER BY sonata_id", (entity["id"],))]
            for group in entity["sonatas"]:
                group["effects"] = [dict(r) for r in conn.execute(
                    "SELECT pieces, desc FROM echo_sonata_effects WHERE echo_id=? AND sonata_id=? ORDER BY pieces",
                    (entity["id"], group["sonata_id"]))]
            detail_source = "echo_detail"
    # JSON 字段还原为数组/对象,避免模型把编码后的字符串误读成正文。
    json_fields = {"echo": ["ranks", "groups"], "item": ["tags", "tag_nums", "extra_json"],
                   "monster": ["extra_json"], "hyperlink": ["params"]}
    for field in json_fields.get(entity_type, []):
        entity[field] = json.loads(entity[field]) if entity.get(field) else None
    for effect in entity.get("effects", []):
        effect["params"] = json.loads(effect["params"] or "[]")
    version = conn.execute("SELECT value FROM meta WHERE key='game_version'").fetchone()[0]
    source = (conn.execute("SELECT source_url FROM raw_docs WHERE kind=? AND doc_id=? LIMIT 1",
                           (detail_source, str(entity["id"]))).fetchone() if detail_source else
              conn.execute("SELECT source_url FROM raw_docs WHERE kind=? LIMIT 1", (entity_type,)).fetchone())
    source_path = f"{entity_type}/{entity['id']}.json" if detail_source else f"{entity_type}.json"
    return {"entity_type": entity_type, "entity": entity,
            "source": f"nanoka {version} / {source_path}",
            "source_url": source["source_url"] if source else None}

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_character_stats",
            "description": (
                "查询《鸣潮》角色的面板数值(生命/攻击/防御,按突破阶段与等级)。"
                "当用户问某个角色的属性、面板、数值时使用。数值直接来自数据源,不会估算。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "角色名,如 爱弥斯 / 散华 / Camellya"},
                    "phase": {"type": "integer", "description": "可选,突破阶段 0-6"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_character_profile",
            "description": (
                "查询《鸣潮》角色的档案:生日、性别、所属地区与势力、声优、官方定位标签。"
                "当用户问角色是谁、属于哪个阵营、声优是谁、定位是什么时使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "角色名"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ascension_materials",
            "description": (
                "查询《鸣潮》角色的突破材料清单(按阶段)。"
                "当用户问某个角色突破/升阶需要什么材料、要多少时使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "角色名"},
                    "phase": {"type": "integer", "description": "可选,突破阶段 1-6"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_text",
            "description": (
                "在《鸣潮》知识库的正文里做关键词检索,覆盖角色故事、语音台词、技能描述、"
                "世界观词条、材料说明。当用户问的是剧情、台词、某个术语是什么,"
                "或者你不确定该用哪个精确工具时使用。注意:2 字关键词会退回模糊匹配。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词,建议 3 个字以上"},
                    "limit": {"type": "integer", "description": "返回条数,默认 5,最大 20"},
                },
                "required": ["query"],
            },
        },
    },
]

# 公开的处理器表:形态与 app/agents/tools.py 的 _HANDLERS 一致 ——
# **处理器返回 dict**,序列化由各自的 execute_tool 负责。后端接线时直接
# ``_HANDLERS.update(knowledge.tools.HANDLERS)`` 即可,不需要在两边写适配层。
def _get_entity_relations(entity_type: str, name: str, relation: str | None = None,
                          direction: str = "outgoing", limit: int = 5, offset: int = 0) -> dict:
    return relations.query(connect(), entity_type, name, relation, direction, limit, offset)


HANDLERS: dict[str, Callable[..., dict]] = {
    "get_entity_relations": _get_entity_relations,
    "get_character_stats": _get_character_stats,
    "get_character_profile": _get_character_profile,
    "get_ascension_materials": _get_ascension_materials,
    "search_knowledge_text": _search_knowledge_text,
    "get_character_skills": _get_character_skills,
    "get_knowledge_entity": _get_knowledge_entity,
}

TOOLS.extend([
    {"type": "function", "function": {
        "name": "get_entity_relations",
        "description": "查询实体关联：角色专武/推荐武器、角色或武器突破材料、声骸所属合鸣套装、怪物对应声骸。incoming反查武器推荐给谁、材料用于哪些角色/武器、套装有哪些声骸、声骸对应哪些怪物。只返回关联和来源，不做强度排名；按next_offset分页。专武必须查signature_weapon，不能用推荐第一项推断。",
        "parameters": {"type": "object", "properties": {
            "entity_type": {"type": "string", "enum": list(relations.TABLES)},
            "name": {"type": "string", "description": "完整名称、已有别名或实体ID，不传整句话"},
            "relation": {"type": "string", "enum": list(relations.RELATIONS)},
            "direction": {"type": "string", "enum": ["outgoing", "incoming"], "description": "默认outgoing"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "默认5"},
            "offset": {"type": "integer", "minimum": 0, "description": "默认0"}}, "required": ["entity_type", "name"]}}},
    {"type": "function", "function": {
        "name": "get_character_skills", "description": "查询角色技能和共鸣链。仅 name 返回目录，随后必须用 tree_id 取单技能或 chain_no 取单链详情；长描述依 next_offset 续读。涉及倍率须明确 level，默认是1级。",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}, "tree_id": {"type": "string", "description": "可选的技能树节点 id"},
            "chain_no": {"type": "integer", "minimum": 1, "maximum": 6, "description": "单条共鸣链序号，与 tree_id 二选一"},
            "offset": {"type": "integer", "minimum": 0, "description": "描述原文字符偏移，默认0；分页不完整时依 next_offset 续读"},
            "level": {"type": "integer", "minimum": 1, "maximum": 20, "description": "默认 1"}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "get_knowledge_entity", "description": "按精确名称、英文别名或id查询武器、声骸、合鸣套装、怪物、材料和术语。武器支持角色名+的专武/专属武器，但仅解析已核对关系。只知道角色需查装备时先用get_entity_relations；推荐不等于专武。",
        "parameters": {"type": "object", "properties": {
            "entity_type": {"type": "string", "enum": ["weapon", "echo", "sonata", "monster", "item", "hyperlink"]},
            "name": {"type": "string"},
            "level": {"type": "integer", "minimum": 1, "maximum": 90, "description": "武器等级,默认 90"},
            "phase": {"type": "integer", "description": "可选武器突破阶段,用于区分突破前后"},
            "refinement": {"type": "integer", "minimum": 1, "maximum": 5, "description": "武器谐振阶级,默认 1"},
            "skill_rank": {"type": "integer", "minimum": 1, "maximum": 5, "description": "声骸技能档位,默认 5;不是声骸品质"}},
            "required": ["entity_type", "name"]}}},
])

_HANDLERS = HANDLERS


def execute_tool(name: str, arguments: str | dict) -> str:
    """执行工具并把结果序列化成 JSON 字符串(喂回模型)。

    与 app/agents/tools.py 的同名函数保持一致的契约:永不抛异常,错误也走 JSON。
    """
    handler = _HANDLERS.get(name)
    if handler is None:
        return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)
    try:
        if isinstance(arguments, str):
            kwargs = json.loads(arguments) if arguments else {}
        else:
            kwargs = dict(arguments or {})
        return json.dumps(handler(**kwargs), ensure_ascii=False, default=str)
    except KnowledgeBaseMissing as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
