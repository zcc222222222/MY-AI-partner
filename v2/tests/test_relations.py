"""Association semantics and provenance, using an isolated small SQLite database."""
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from knowledge import db, relations, tools


class RelationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.init_schema(self.conn)
        db.set_meta(self.conn, "game_version", "3.7")
        self.conn.executemany("INSERT INTO characters(id,name,game_version) VALUES (?,?,?)",
                              [(1210, "爱弥斯", "3.7"), (1102, "散华", "3.7")])
        self.conn.executemany("INSERT INTO weapons(id,name) VALUES (?,?)",
                              [(21020076, "永远的启明星"), (21020015, "千古洑流")])
        self.conn.execute("INSERT INTO weapon_details(weapon_id,name,is_skin) VALUES (21020076,'永远的启明星',0)")
        self.conn.execute("INSERT INTO weapon_effects VALUES (21020076,5,'谐振5阶测试效果','[]')")
        self.conn.execute("INSERT INTO weapon_stats VALUES (21020076,1,20,0,'攻击',100,0,0)")
        self.conn.executemany("INSERT INTO character_recommend VALUES (?,'weapon',?,?, '')",
                              [(1210, 0, 21020076), (1210, 1, 21020015), (1102, 0, 21020015)])
        self.conn.execute("INSERT INTO aliases VALUES ('Aemeath','character','1210','en','test')")
        self.conn.execute("INSERT INTO echoes(id,name) VALUES (10,'测试声骸')")
        self.conn.execute("INSERT INTO sonatas(id,name) VALUES (20,'测试合鸣')")
        self.conn.execute("INSERT INTO echo_sonatas VALUES (10,20,'测试合鸣')")
        self.conn.execute("INSERT INTO monsters(id,name,echo_id) VALUES (30,'测试怪物',10)")
        self.conn.execute("INSERT INTO character_ascensions VALUES (1210,1,99,2)")
        self.conn.execute("INSERT INTO character_ascensions VALUES (1210,2,99,3)")
        for kind, doc_id in (("character", "1210"), ("character", "1102"), ("echo_detail", "10"), ("monster", "monster")):
            self.conn.execute("INSERT INTO raw_docs(kind,doc_id,game_version,payload,sha256,source_url) VALUES (?,?,'3.7','{}','test',?)",
                              (kind, doc_id, f"https://example.invalid/{kind}/{doc_id}"))
        relations.rebuild(self.conn, "3.7", [])
        self.mock_connect = patch.object(tools, "connect", return_value=self.conn)
        self.mock_connect.start()

    def tearDown(self):
        self.mock_connect.stop()
        self.conn.close()

    def test_rover_variants_never_resolve_to_arbitrary_first_record(self):
        for ident, sex in [(1501, "男"), (1502, "女")]:
            self.conn.execute("INSERT INTO characters(id,name,game_version) VALUES (?, '漂泊者·衍射', '3.7')", (ident,))
            self.conn.execute("INSERT INTO character_profiles(character_id,sex) VALUES (?,?)", (ident, sex))
            self.conn.execute("INSERT INTO aliases VALUES (?,'character',?,'zh','test')", (f"漂泊者·衍射·{sex}", str(ident)))
        ambiguous = relations.resolve(self.conn, "character", "漂泊者·衍射")
        self.assertEqual(ambiguous["total_candidates"], 2)
        self.assertEqual({r["sex"] for r in ambiguous["candidates"]}, {"男", "女"})
        self.assertIsNone(tools.resolve_character(self.conn, "漂泊者·衍射"))
        self.assertIsNone(tools.resolve_character(self.conn, "漂泊者"))
        self.assertEqual(tools.resolve_character(self.conn, "1502")["id"], 1502)
        self.assertEqual(relations.resolve(self.conn, "character", "漂泊者·衍射·女")["id"], 1502)
        self.assertIn("error", relations.resolve(self.conn, "character", "我"))

    def test_canonical_form_name_overrides_other_forms_legacy_nickname(self):
        self.conn.executemany("INSERT INTO characters(id,name,game_version) VALUES (?,?,'3.7')", [(1309, "漂泊者·导电"), (1406, "漂泊者·气动"), (1408, "漂泊者·气动")])
        self.conn.execute("INSERT INTO aliases VALUES ('漂泊者·气动','character','1309','zh','legacy_nickname')")
        result = relations.resolve(self.conn, "character", "漂泊者·气动")
        self.assertEqual({r["id"] for r in result["candidates"]}, {1406, 1408})
        self.assertIsNone(tools.resolve_character(self.conn, "漂泊者·气动"))

    def test_reviewed_signature_and_recommendation_are_different(self):
        sig = relations.query(self.conn, "character", "Aemeath", "signature_weapon")
        self.assertEqual(sig["relations"][0]["to_id"], "21020076")
        self.assertEqual(sig["relations"][0]["evidence_kind"], "reviewed_community")
        self.assertIn("wiki.biligame.com", sig["relations"][0]["source_url"])
        self.assertEqual(relations.query(self.conn, "character", "散华", "signature_weapon")["total"], 0)
        rec = relations.query(self.conn, "character", "1210", "recommended_weapon")
        self.assertEqual(rec["total"], 2)
        self.assertTrue(all(r["relation"] != "signature_weapon" for r in rec["relations"]))

    def test_shorthand_preserves_parameters_and_resolution_evidence(self):
        for name in ("爱弥斯的专武", "爱弥斯专属武器", "Aemeath的专武"):
            result = tools._get_knowledge_entity("weapon", name, level=20, refinement=5)
            self.assertEqual(result["entity"]["id"], 21020076)
            self.assertEqual(result["resolved_from"]["relation"], "signature_weapon")
            self.assertEqual(result["entity"]["refinement"], 5)
            self.assertEqual(result["entity"]["effect"], "谐振5阶测试效果")
            self.assertEqual(result["entity"]["stats"][0]["level"], 20)
        self.assertIn("error", tools._get_knowledge_entity("weapon", "散华的专武"))
        self.assertIn("error", tools._get_knowledge_entity("weapon", "专武"))

    def test_reverse_query_pagination_has_no_duplicates(self):
        first = relations.query(self.conn, "weapon", "千古洑流", "recommended_weapon", "incoming", 1)
        second = relations.query(self.conn, "weapon", "21020015", "recommended_weapon", "incoming", 1, first["next_offset"])
        self.assertEqual(first["total"], 2)
        self.assertIsNone(second["next_offset"])
        self.assertNotEqual(first["relations"][0]["from_id"], second["relations"][0]["from_id"])

    def test_source_field_and_dangling_material_are_preserved(self):
        result = relations.query(self.conn, "character", "1210", "ascension_material")
        self.assertEqual(result["total"], 1)  # Same material in two phases, no invented total.
        edge = result["relations"][0]
        self.assertFalse(edge["related_entity"]["resolved"])
        self.assertEqual(edge["source_url"], "https://example.invalid/character/1210")
        reverse = relations.query(self.conn, "item", "99", "ascension_material", "incoming")
        self.assertEqual(reverse["relations"][0]["related_entity"]["name"], "爱弥斯")

    def test_echo_sonata_and_monster_echo_both_directions(self):
        for etype, name, relation, target in (("echo", "10", "sonata", "20"), ("monster", "30", "echo", "10")):
            result = relations.query(self.conn, etype, name, relation)
            self.assertEqual(result["relations"][0]["to_id"], target)
            edge = result["relations"][0]
            inverse = relations.query(self.conn, edge["to_type"], target, relation, "incoming")
            self.assertEqual(inverse["relations"][0]["from_id"], name)

    def test_ambiguous_alias_never_picks_first_character(self):
        self.conn.executemany("INSERT INTO aliases VALUES (?,'character',?,'','test')", [("同名", "1210"), ("同名", "1102")])
        result = relations.query(self.conn, "character", "同名")
        self.assertIn("error", result)
        self.assertEqual(result["total_candidates"], 2)

    def test_curated_relations_version_scope_and_rebuild_idempotence(self):
        count = self.conn.execute("SELECT count(*) FROM entity_relations").fetchone()[0]
        digest = relations.rebuild(self.conn, "3.7", [])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM entity_relations").fetchone()[0], count)
        self.assertEqual(len(digest), 64)
        relations.rebuild(self.conn, "3.8", [])
        self.assertEqual(relations.query(self.conn, "character", "1210", "signature_weapon")["total"], 0)

    def test_invalid_type_direction_and_offset_are_errors(self):
        for kwargs in ({"entity_type": "invalid"}, {"relation": "best_team"}, {"direction": "both"}, {"offset": -1}):
            params = {"entity_type": "character", "name": "1210", **kwargs}
            self.assertIn("error", relations.query(self.conn, **params))

    def test_curated_wrong_endpoint_name_aborts_instead_of_linking(self):
        self.conn.execute("UPDATE weapons SET name='另一个武器' WHERE id=21020076")
        with self.assertRaisesRegex(ValueError, "名称不一致"):
            relations.rebuild(self.conn, "3.7", [])

    def test_app_registry_exposes_relation_lookup_and_shorthand(self):
        from app.agents.tools import TOOLS, execute_tool
        self.assertIn("get_entity_relations", [t["function"]["name"] for t in TOOLS])
        result = json.loads(execute_tool("get_entity_relations", json.dumps({
            "entity_type": "character", "name": "1210", "relation": "signature_weapon"})))
        self.assertEqual(result["relations"][0]["to_id"], "21020076")
        entity = json.loads(execute_tool("get_knowledge_entity", json.dumps({
            "entity_type": "weapon", "name": "爱弥斯的专武", "refinement": 5})))
        self.assertEqual(entity["entity"]["effect"], "谐振5阶测试效果")


if __name__ == "__main__":
    unittest.main()
