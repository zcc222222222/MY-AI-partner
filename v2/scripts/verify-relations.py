"""Offline verification of published association coverage and implicit-name resolution."""
import hashlib
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from knowledge import relations, tools

conn = tools.connect()
checks = {
    "recommended_weapon": "SELECT count(*) FROM character_recommend WHERE kind='weapon'",
    "echo": "SELECT count(*) FROM monsters WHERE echo_id>0",
    "sonata": "SELECT count(*) FROM echo_sonatas",
    "ascension_material": "SELECT (SELECT count(*) FROM (SELECT DISTINCT character_id,item_key FROM character_ascensions)) + "
                          "(SELECT count(*) FROM (SELECT DISTINCT weapon_id,item_key FROM weapon_ascensions))",
}
for kind, sql in checks.items():
    expected = conn.execute(sql).fetchone()[0]
    actual = conn.execute("SELECT count(*) FROM entity_relations WHERE relation=?", (kind,)).fetchone()[0]
    assert actual == expected, (kind, actual, expected)
    print("PASS", kind, actual)
assert conn.execute("SELECT count(*) FROM entity_relations WHERE source_url IS NULL").fetchone()[0] == 0
assert conn.execute("SELECT value FROM meta WHERE key='relations_curation_sha256'").fetchone()[0] == hashlib.sha256(relations.CURATED_PATH.read_bytes()).hexdigest()
for row in conn.execute("SELECT * FROM entity_relations WHERE evidence_kind='source_field'"):
    if row["relation"] == "recommended_weapon":
        assert conn.execute("SELECT 1 FROM character_recommend WHERE character_id=? AND ref_id=? AND kind='weapon'",
                            (row["from_id"], row["to_id"])).fetchone()
    elif row["relation"] == "sonata":
        assert conn.execute("SELECT 1 FROM echo_sonatas WHERE echo_id=? AND sonata_id=?", (row["from_id"], row["to_id"])).fetchone()
    elif row["relation"] == "echo":
        assert conn.execute("SELECT 1 FROM monsters WHERE id=? AND echo_id=?", (row["from_id"], row["to_id"])).fetchone()
    elif row["relation"] == "ascension_material":
        table, owner = ("character_ascensions", "character_id") if row["from_type"] == "character" else ("weapon_ascensions", "weapon_id")
        assert conn.execute(f"SELECT 1 FROM {table} WHERE {owner}=? AND item_key=?", (row["from_id"], row["to_id"])).fetchone()
print("PASS source edges, provenance and curation checksum")
for row in conn.execute("SELECT * FROM entity_relations WHERE relation='signature_weapon'"):
    result = tools._get_knowledge_entity("weapon", row["from_id"] + "的专武", refinement=5, level=20)
    assert str(result["entity"]["id"]) == row["to_id"]
    assert result["entity"]["refinement"] == 5
print("PASS reviewed signature shorthand and refinement passthrough")
tools.close()
