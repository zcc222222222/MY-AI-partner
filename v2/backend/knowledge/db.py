"""知识库表结构(SQLite)。

分层原则(和架构讨论一致):
  raw_docs    原文层 —— 每个源文件原样存一份。**永不原地改**。
              归一化逻辑写错了,只重建上面几层,不需要重新联网抓一遍。
  结构化表    确定性字段。这一层回答"能用 SQL 写出来的问题",不经过检索、不经过 LLM。
  aliases     实体别名 -> 实体。中文里 2 字实体名(今州/散华/长离)走这里精确匹配,
              不走全文检索 —— 实测 trigram 对 2 字查询失效。
  refs        <te href> 抽出来的实体关系边。知识图谱的原料,不需要图数据库。
  text_index  正文(供短查询 LIKE 兜底)
  fts_text    FTS5 trigram 全文索引(仅 >=3 字查询有效,实测结论)

关于数据库选型:这里是 SQLite,因为知识库是"8-10MB、只读为主、可随时重建"的东西,
和用户域(写多、不能丢)是两种负载。DDL 尽量写成 MySQL 也能用的形态,
唯一不可移植的是 FTS5 —— 迁 MySQL 时换成 FULLTEXT ... WITH PARSER ngram。
"""
import os
import sqlite3

# FTS5 trigram 需要 SQLite >= 3.34。低版本就没有全文索引这一层,其余功能不受影响。
MIN_SQLITE_FOR_TRIGRAM = (3, 34, 0)

CORE_DDL = [
    """CREATE TABLE IF NOT EXISTS entity_relations (
        from_type TEXT NOT NULL, from_id TEXT NOT NULL,
        relation TEXT NOT NULL, to_type TEXT NOT NULL, to_id TEXT NOT NULL,
        source_field TEXT NOT NULL, source_url TEXT,
        evidence_kind TEXT NOT NULL, game_version TEXT NOT NULL,
        PRIMARY KEY(from_type, from_id, relation, to_type, to_id, source_field)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_relations_target
        ON entity_relations(to_type, to_id, relation)""",
    # ---------- 元信息 ----------
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """,
    # ---------- 原文层 ----------
    """
    CREATE TABLE IF NOT EXISTS raw_docs (
        kind         TEXT NOT NULL,
        doc_id       TEXT NOT NULL,
        game_version TEXT NOT NULL,
        locale       TEXT NOT NULL DEFAULT '',
        payload      TEXT NOT NULL,
        sha256       TEXT NOT NULL,
        source_url   TEXT,
        fetched_at   TEXT,
        PRIMARY KEY (kind, doc_id, game_version, locale)
    )
    """,
    # ---------- 角色 ----------
    """
    CREATE TABLE IF NOT EXISTS characters (
        id           INTEGER PRIMARY KEY,
        name         TEXT,
        nick_name    TEXT,
        rarity       INTEGER,
        element      INTEGER,
        weapon       INTEGER,
        element_name TEXT,
        weapon_name  TEXT,
        game_version TEXT,
        tag_summary  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS character_profiles (
        character_id        INTEGER PRIMARY KEY,
        birth               TEXT,
        sex                 TEXT,
        country             TEXT,
        influence           TEXT,
        talent_name         TEXT,
        talent_doc          TEXT,
        talent_certification TEXT,
        cv_cn               TEXT,
        cv_jp               TEXT,
        cv_ko               TEXT,
        cv_en               TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS character_tags (
        character_id INTEGER NOT NULL,
        tag_id       TEXT NOT NULL,
        name         TEXT,
        desc         TEXT,
        color        TEXT,
        PRIMARY KEY (character_id, tag_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS character_stats (
        character_id INTEGER NOT NULL,
        phase        INTEGER NOT NULL,
        stage        INTEGER NOT NULL,
        life         REAL,
        atk          REAL,
        def          REAL,
        PRIMARY KEY (character_id, phase, stage)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS character_skills (
        character_id INTEGER NOT NULL,
        tree_id      TEXT NOT NULL,
        node_type    INTEGER,
        name         TEXT,
        desc         TEXT,
        desc_params  TEXT,
        consumes     TEXT,
        PRIMARY KEY (character_id, tree_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS character_chains (
        character_id INTEGER NOT NULL,
        chain_no     INTEGER NOT NULL,
        name         TEXT,
        desc         TEXT,
        desc_params  TEXT,
        PRIMARY KEY (character_id, chain_no)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS character_stories (
        character_id INTEGER NOT NULL,
        idx          INTEGER NOT NULL,
        title        TEXT,
        content      TEXT,
        PRIMARY KEY (character_id, idx)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS character_voices (
        character_id INTEGER NOT NULL,
        idx          INTEGER NOT NULL,
        voice_type   INTEGER,
        title        TEXT,
        content      TEXT,
        voice_asset  TEXT,
        PRIMARY KEY (character_id, idx)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS character_ascensions (
        character_id INTEGER NOT NULL,
        phase        INTEGER NOT NULL,
        item_key     INTEGER NOT NULL,
        amount       INTEGER,
        PRIMARY KEY (character_id, phase, item_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS character_goods (
        character_id INTEGER NOT NULL,
        idx          INTEGER NOT NULL,
        title        TEXT,
        content      TEXT,
        PRIMARY KEY (character_id, idx)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS character_recommend (
        character_id INTEGER NOT NULL,
        kind         TEXT NOT NULL,
        idx          INTEGER NOT NULL,
        ref_id       INTEGER,
        raw          TEXT,
        PRIMARY KEY (character_id, kind, idx)
    )
    """,
    # 特色料理。54/64 个角色有,是扁平结构,单独一表。
    """
    CREATE TABLE IF NOT EXISTS character_special_cook (
        character_id INTEGER PRIMARY KEY,
        cook_id      INTEGER,
        name         TEXT,
        desc         TEXT,
        rarity       INTEGER
    )
    """,
    # 弱点/韧性相关数值。64/64 个角色都有,五个整数。
    """
    CREATE TABLE IF NOT EXISTS character_weakness (
        character_id          INTEGER PRIMARY KEY,
        weakness_build_up     INTEGER,
        weakness_build_up_max INTEGER,
        weakness_total_bonus  INTEGER,
        break_weakness_ratio  INTEGER,
        weakness_mastery      INTEGER
    )
    """,
    # 100 级经验曲线。源里是长度 100 的整数数组,level = 下标 + 1。
    """
    CREATE TABLE IF NOT EXISTS character_level_exp (
        character_id INTEGER NOT NULL,
        level        INTEGER NOT NULL,
        exp          INTEGER,
        PRIMARY KEY (character_id, level)
    )
    """,
    # 皮肤。只取有语义的字段,资源路径(portrait/spine 等)不入库 —— 它们是
    # UE 资源引用,不是可直接使用的 URL,存了也没法渲染。
    """
    CREATE TABLE IF NOT EXISTS character_skins (
        character_id   INTEGER NOT NULL,
        skin_id        INTEGER NOT NULL,
        name           TEXT,
        title_name     TEXT,
        sub_dec_name   TEXT,
        bg_description TEXT,
        quality_id     INTEGER,
        sort_index     INTEGER,
        PRIMARY KEY (character_id, skin_id)
    )
    """,
    # 共鸣模态分支。只有 5/64 个角色有(新机制角色),但结构清晰。
    """
    CREATE TABLE IF NOT EXISTS character_skill_branches (
        character_id INTEGER NOT NULL,
        branch_id    INTEGER NOT NULL,
        name         TEXT,
        desc         TEXT,
        PRIMARY KEY (character_id, branch_id)
    )
    """,
    # forte_new.features:扁平的特性描述列表(20/64 个角色有)。
    """
    CREATE TABLE IF NOT EXISTS character_forte_features (
        character_id INTEGER NOT NULL,
        idx          INTEGER NOT NULL,
        feature      TEXT,
        PRIMARY KEY (character_id, idx)
    )
    """,
    # 兜底:结构太深、不值得为它设计表的东西(如 forte_new.instructions 的三层嵌套)。
    # 存 JSON 而不是丢掉 —— "没建模"和"没数据"是两件事,不该混为一谈。
    """
    CREATE TABLE IF NOT EXISTS character_extras (
        character_id INTEGER NOT NULL,
        kind         TEXT NOT NULL,
        payload      TEXT,
        PRIMARY KEY (character_id, kind)
    )
    """,
    # ---------- 其他实体 ----------
    """
    CREATE TABLE IF NOT EXISTS weapons (
        id           INTEGER PRIMARY KEY,
        name         TEXT,
        en           TEXT,
        rarity       INTEGER,
        weapon_type  INTEGER,
        atk          INTEGER,
        sub          TEXT,
        desc         TEXT,
        game_version TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS echoes (
        id           INTEGER PRIMARY KEY,
        name         TEXT,
        en           TEXT,
        code         TEXT,
        ranks        TEXT,
        groups       TEXT,
        intensity    INTEGER,
        game_version TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sonatas (
        id           INTEGER PRIMARY KEY,
        name         TEXT,
        set_text     TEXT,
        game_version TEXT
    )
    """,
    # sonata.json 的 set 是 {件数: {语言: {desc, param}}} 的嵌套结构,
    # 压成一个字符串会丢掉"2 件套 / 5 件套"这个关键区分,所以单独一张表。
    """
    CREATE TABLE IF NOT EXISTS sonata_effects (
        sonata_id INTEGER NOT NULL,
        pieces    INTEGER NOT NULL,
        desc      TEXT,
        params    TEXT,
        PRIMARY KEY (sonata_id, pieces)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS monsters (
        id           INTEGER PRIMARY KEY,
        name         TEXT,
        en           TEXT,
        rarity       INTEGER,
        element      INTEGER,
        echo_id      INTEGER,
        game_version TEXT,
        extra_json   TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS items (
        id           INTEGER PRIMARY KEY,
        name         TEXT,
        en           TEXT,
        rarity       INTEGER,
        item_type    TEXT,
        desc         TEXT,
        tags         TEXT,
        tag_nums     TEXT,
        game_version TEXT,
        extra_json   TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hyperlinks (
        id      INTEGER PRIMARY KEY,
        name    TEXT,
        desc    TEXT,
        params  TEXT
    )
    """,
    # ---------- 别名 / 关系 / 检索 ----------
    """
    CREATE TABLE IF NOT EXISTS aliases (
        alias       TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        entity_id   TEXT NOT NULL,
        locale      TEXT NOT NULL DEFAULT '',
        source      TEXT,
        PRIMARY KEY (alias, entity_type, entity_id, locale)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS refs (
        from_type       TEXT NOT NULL,
        from_id         TEXT NOT NULL,
        to_hyperlink_id INTEGER NOT NULL,
        PRIMARY KEY (from_type, from_id, to_hyperlink_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS text_index (
        entity_type TEXT NOT NULL,
        entity_id   TEXT NOT NULL,
        field       TEXT NOT NULL,
        body        TEXT NOT NULL,
        PRIMARY KEY (entity_type, entity_id, field)
    )
    """,
]

# 新增表而不更改既有列,旧知识库可通过重建或 --keep 升级。
CORE_DDL += [
    """CREATE TABLE IF NOT EXISTS character_skill_details (
        character_id INTEGER NOT NULL, tree_id TEXT NOT NULL,
        skill_type TEXT, simple_desc TEXT, parent_nodes TEXT,
        coordinate INTEGER, unlock_condition INTEGER, branch_ids TEXT,
        damage TEXT, source_payload TEXT NOT NULL,
        PRIMARY KEY (character_id, tree_id)
    )""",
    """CREATE TABLE IF NOT EXISTS character_skill_levels (
        character_id INTEGER NOT NULL, tree_id TEXT NOT NULL,
        attribute_id TEXT NOT NULL, name TEXT, params TEXT, format TEXT,
        PRIMARY KEY (character_id, tree_id, attribute_id)
    )""",
    """CREATE TABLE IF NOT EXISTS character_skill_costs (
        character_id INTEGER NOT NULL, tree_id TEXT NOT NULL,
        level INTEGER NOT NULL, idx INTEGER NOT NULL,
        item_key INTEGER NOT NULL, amount INTEGER,
        PRIMARY KEY (character_id, tree_id, level, idx)
    )""",
    """CREATE TABLE IF NOT EXISTS text_sources (
        entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
        field TEXT NOT NULL, raw_text TEXT, params TEXT,
        PRIMARY KEY (entity_type, entity_id, field)
    )""",
    """CREATE TABLE IF NOT EXISTS entity_extras (
        entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
        payload TEXT NOT NULL,
        PRIMARY KEY (entity_type, entity_id)
    )""",
]

CORE_DDL += [
    """CREATE TABLE IF NOT EXISTS weapon_details (
        weapon_id INTEGER PRIMARY KEY, name TEXT, desc TEXT, effect_name TEXT,
        effect_template TEXT, effect_params TEXT, is_skin INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS weapon_stats (
        weapon_id INTEGER NOT NULL, phase INTEGER NOT NULL, level INTEGER NOT NULL,
        idx INTEGER NOT NULL, name TEXT, raw_value REAL, is_ratio INTEGER, is_percent INTEGER,
        PRIMARY KEY (weapon_id, phase, level, idx)
    )""",
    """CREATE TABLE IF NOT EXISTS weapon_effects (
        weapon_id INTEGER NOT NULL, refinement INTEGER NOT NULL, desc TEXT, params TEXT,
        PRIMARY KEY (weapon_id, refinement)
    )""",
    """CREATE TABLE IF NOT EXISTS weapon_ascensions (
        weapon_id INTEGER NOT NULL, phase INTEGER NOT NULL, idx INTEGER NOT NULL,
        item_key INTEGER, amount INTEGER,
        PRIMARY KEY (weapon_id, phase, idx)
    )""",
    """CREATE TABLE IF NOT EXISTS echo_details (
        echo_id INTEGER PRIMARY KEY, name TEXT, type TEXT, intensity TEXT, place TEXT,
        intensity_code INTEGER, monster_info INTEGER, rarity TEXT, skill_template TEXT,
        simple_desc TEXT, skill_params TEXT, damage TEXT, phantom TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS echo_skills (
        echo_id INTEGER NOT NULL, skill_rank INTEGER NOT NULL, desc TEXT, params TEXT,
        PRIMARY KEY (echo_id, skill_rank)
    )""",
    """CREATE TABLE IF NOT EXISTS echo_sonatas (
        echo_id INTEGER NOT NULL, sonata_id INTEGER NOT NULL, name TEXT,
        PRIMARY KEY (echo_id, sonata_id)
    )""",
    """CREATE TABLE IF NOT EXISTS echo_sonata_effects (
        echo_id INTEGER NOT NULL, sonata_id INTEGER NOT NULL, pieces INTEGER NOT NULL,
        desc TEXT, params TEXT,
        PRIMARY KEY (echo_id, sonata_id, pieces)
    )""",
]

INDEX_DDL = [
    "CREATE INDEX IF NOT EXISTS ix_aliases_alias ON aliases(alias)",
    "CREATE INDEX IF NOT EXISTS ix_refs_from ON refs(from_type, from_id)",
    "CREATE INDEX IF NOT EXISTS ix_refs_to ON refs(to_hyperlink_id)",
    "CREATE INDEX IF NOT EXISTS ix_text_index_entity ON text_index(entity_type, entity_id)",
    "CREATE INDEX IF NOT EXISTS ix_skills_char ON character_skills(character_id)",
    "CREATE INDEX IF NOT EXISTS ix_voices_char ON character_voices(character_id)",
]

FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS fts_text USING fts5(
    entity_type,
    entity_id UNINDEXED,
    field UNINDEXED,
    body,
    tokenize='trigram'
)
"""


def connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def fts5_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x)")
        conn.execute("DROP TABLE _fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def trigram_available(conn: sqlite3.Connection) -> bool:
    if sqlite3.sqlite_version_info < MIN_SQLITE_FOR_TRIGRAM:
        return False
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _tg_probe USING fts5(x, tokenize='trigram')")
        conn.execute("DROP TABLE _tg_probe")
        return True
    except sqlite3.OperationalError:
        return False


def init_schema(conn: sqlite3.Connection, *, reset: bool = False) -> dict:
    """建表。reset=True 时先清空所有表 —— 知识库是可重建的,这是它的权利。

    返回一份能力报告,调用方应当打印出来:如果这台机器没有 trigram,
    全文检索会退化,这个事实必须是可见的而不是静默的。
    """
    if reset:
        drop_all(conn)

    for ddl in CORE_DDL + INDEX_DDL:
        conn.execute(ddl)

    has_fts = fts5_available(conn)
    has_trigram = has_fts and trigram_available(conn)
    if has_trigram:
        conn.execute(FTS_DDL)

    conn.commit()
    capability = {
        "sqlite_version": sqlite3.sqlite_version,
        "fts5": has_fts,
        "trigram": has_trigram,
    }
    for k, v in capability.items():
        set_meta(conn, f"capability.{k}", str(v))
    return capability


def all_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]


def drop_all(conn: sqlite3.Connection) -> None:
    """删掉全部表(含 FTS 影子表)。用于重建 —— 知识库不含用户数据,可以整库重来。"""
    conn.execute("PRAGMA foreign_keys=OFF")
    for name in all_tables(conn):
        if name.startswith("sqlite_"):
            continue
        conn.execute(f'DROP TABLE IF EXISTS "{name}"')
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default
