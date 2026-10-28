#!/usr/bin/env python3
"""阶段 1 回归测试。**完全离线**:不联网、不需要真库,从 tests/fixtures 建一个临时库。

和 scripts/verify-stage1.py 的分工:
  verify-stage1.py  跑在你**真实镜像出来**的库上,检查数据质量与规模(会随版本变化)
  test_stage1.py    跑在**固定样本**上,检查代码契约(不该随版本变化,也不该依赖网络)

后者是将来接 CI 的那一半 —— 它不需要密钥、不需要外网,和仓库既有的
scripts/verify-p0.py 是同一个定位。

用法:
    python v2/tests/test_stage1.py          # 直接跑,输出 unittest 风格
    python -m unittest discover v2/tests    # 也可以用 unittest 发现
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 知识库包在 <应用根>/backend/knowledge —— 与应用根同名与否无关。
sys.path.insert(0, os.path.join(APP_ROOT, "backend"))

from knowledge import build as kb_build  # noqa: E402
from knowledge import audit, config, db, equipment, nanoka, normalize, parse, tools  # noqa: E402
import urllib.error
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "raw")


def fixture_available() -> bool:
    return os.path.isdir(FIXTURES)


class TestParse(unittest.TestCase):
    """解析器是纯函数,所以这组测试不需要任何 fixture。"""

    def test_te_href_extracted_and_text_kept(self):
        p = parse.parse("隶属于<te href=850091>今州</te>边庭")
        self.assertEqual(p.refs, [850091])
        self.assertIn("今州", p.text)
        self.assertNotIn("<te", p.text)

    def test_multiple_refs_in_order(self):
        p = parse.parse("<te href=1>甲</te>和<te href=2>乙</te>")
        self.assertEqual(p.refs, [1, 2])

    def test_refs_deduped_but_ordered(self):
        p = parse.parse("<te href=2>乙</te><te href=1>甲</te><te href=2>乙</te>")
        self.assertEqual(p.refs, [2, 1])

    def test_rich_text_tags_stripped(self):
        p = parse.parse("<size=40><color=Title>普攻</color></size>造成伤害")
        self.assertNotIn("<", p.text)
        self.assertIn("普攻", p.text)
        self.assertIn("造成伤害", p.text)

    def test_unknown_tag_stripped(self):
        # 源里出现过裸 <c=Highlight>,枚举式白名单会把它漏在正文里
        p = parse.parse("造成<c=Highlight>额外伤害")
        self.assertNotIn("<", p.text)

    def test_params_recorded_but_kept(self):
        p = parse.parse("提升{2},超过{1}秒")
        self.assertEqual(p.params, [1, 2])
        self.assertIn("{2}", p.text)

    def test_none_and_empty_safe(self):
        self.assertEqual(parse.parse(None).text, "")
        self.assertEqual(parse.parse("").refs, [])

    def test_resolve_refs_no_duplicate_when_same(self):
        self.assertEqual(parse.resolve_refs("<te href=1>今州</te>", {1: "今州"}), "今州")

    def test_resolve_refs_unknown_id_keeps_text(self):
        self.assertEqual(parse.resolve_refs("<te href=99>未知</te>", {}), "未知")

    def test_params_use_zero_based_source_values(self):
        self.assertEqual(parse.resolve_params("提升{0},持续{1}秒", ["15%", "10"]), "提升15%,持续10秒")
        self.assertEqual(parse.resolve_params("{2}/{0:0.0%}/{1}", ["1", None]), "{2}/{0:0.0%}/{1}")

    def test_unknown_source_fields_are_reported(self):
        result = audit.inspect("echo", {"1": {"zh": "测试", "new_field": 1}})
        self.assertEqual(result["unknown_fields"], ["new_field"])

    def test_sonata_multilingual_aliases_and_params(self):
        norm = normalize.Normalized()
        normalize.normalize_sonatas({"1": {"name": {"zh": "测试", "en": "Test"},
            "set": {"2": {"zh": {"desc": "提升{0}", "param": ["10%"]}}}}}, "3.7", "zh", norm)
        self.assertEqual(norm.tables["sonata_effects"][0]["desc"], "提升10%")
        self.assertEqual({r["alias"] for r in norm.tables["aliases"]}, {"测试", "Test"})

    def test_missing_local_mirror_is_fetched_without_cache_headers(self):
        plan = nanoka.FilePlan("roster", "character.json", "https://example.invalid/character.json", "character.json")
        report = nanoka.MirrorReport()
        with patch.object(nanoka.os.path, "isfile", return_value=False), \
             patch.object(nanoka, "_get_bytes", return_value=(b'{"1":{}}', {})) as get, \
             patch.object(nanoka, "_write_atomic"):
            nanoka._run_plans([plan], "3.7", {"character.json": {"etag": "old", "last_modified": "old"}},
                              report, 0, False, 10, None)
        self.assertIsNone(get.call_args.kwargs["etag"])
        self.assertIsNone(get.call_args.kwargs["last_modified"])
        self.assertEqual(report.fetched, ["character.json"])


class TestMirror(unittest.TestCase):
    def test_missing_refinement_parameter_is_not_guessed(self):
        norm = normalize.Normalized()
        equipment.normalize_weapon({"id": 1, "effect": "{0}/{1}", "param": [["a1", "a2"], ["b1"]]}, norm)
        self.assertEqual(norm.tables["weapon_effects"][1]["desc"], "a2/{1}")
        self.assertTrue(norm.notes)

    def test_corrupt_cached_file_is_refetched_without_etag(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "one.json")
            with open(path, "wb") as fh:
                fh.write(b"corrupted")
            plan = nanoka.FilePlan("weapon_detail", "1", "https://example.invalid/1", "one.json")
            with patch.object(nanoka, "local_path", return_value=path), \
                 patch.object(nanoka, "_get_bytes", return_value=(b'{"id":1}', {})) as get, \
                 patch.object(nanoka, "_pause"):
                nanoka._run_plans([plan], "3.7", {"one.json": {"sha256": "oldhash", "etag": "old"}},
                                  nanoka.MirrorReport(), 1.5, False, 10, None, skip_existing=True)
            self.assertIsNone(get.call_args.kwargs["etag"])

    def test_weapon_projection_is_not_treated_as_combat_weapon(self):
        norm = normalize.Normalized()
        equipment.normalize_weapon({"id": 80080001, "name": "projection", "skin": True}, norm)
        self.assertTrue(norm.tables["weapon_details"][0]["is_skin"])
        self.assertNotIn("weapon_stats", norm.tables)
        self.assertNotIn("weapon_effects", norm.tables)

    def test_resume_checks_hash_and_skips_intact_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "one.json")
            body = b'{"id":1}'
            with open(path, "wb") as fh:
                fh.write(body)
            plan = nanoka.FilePlan("weapon_detail", "1", "https://example.invalid/1", "one.json")
            report = nanoka.MirrorReport()
            with patch.object(nanoka, "local_path", return_value=path), patch.object(nanoka, "_get_bytes") as get:
                nanoka._run_plans([plan], "3.7", {"one.json": {"sha256": nanoka._sha256(body)}}, report,
                                  1.5, False, 10, None, skip_existing=True)
            get.assert_not_called()
            self.assertEqual(report.skipped, ["one.json"])

    def test_plans_distinguish_index_and_details(self):
        for kind in ("weapon", "echo"):
            plans = nanoka.plan_details("3.7", "zh", kind, ["123"])
            self.assertEqual(plans[0].kind, kind + "_detail")
            self.assertEqual(plans[0].rel_path, f"zh/{kind}/123.json")

    def test_fast_interval_is_rejected_before_network(self):
        with patch.object(nanoka, "_get_bytes") as get:
            with self.assertRaises(ValueError):
                nanoka.mirror(version="3.7", delay=0.1)
        get.assert_not_called()

    def test_throttle_waits_before_next_request(self):
        with patch.object(nanoka, "_last_request", 10), patch.object(nanoka.time, "monotonic", return_value=10.2), \
             patch.object(nanoka, "_pause") as pause, \
             patch.object(nanoka.urllib.request, "urlopen", side_effect=urllib.error.URLError("test")):
            with self.assertRaises(urllib.error.URLError):
                nanoka._get_bytes("https://example.invalid", timeout=1, request_interval=1.5)
        self.assertAlmostEqual(pause.call_args.args[0], 1.3)

    def test_429_obeys_retry_after_and_checkpoints_success(self):
        plan = nanoka.FilePlan("weapon_detail", "1", "https://example.invalid/1", "zh/weapon/1.json")
        error = urllib.error.HTTPError(plan.url, 429, "limited", {"Retry-After": "12"}, None)
        with patch.object(nanoka, "_get_bytes", side_effect=[error, (b'{"id":1}', {})]) as get, \
             patch.object(nanoka, "_pause") as pause, patch.object(nanoka, "_write_atomic"), \
             patch.object(nanoka.os.path, "isfile", return_value=False), patch.object(nanoka, "save_manifest") as checkpoint:
            nanoka._run_plans([plan], "3.7", {}, nanoka.MirrorReport(), 1.5, False, 10, None, checkpoint)
        self.assertEqual(get.call_count, 2)
        self.assertIn(unittest.mock.call(12.0), pause.call_args_list)
        checkpoint.assert_called_once()

    def test_403_stops_batch_without_requesting_next_file(self):
        plan = nanoka.FilePlan("weapon_detail", "1", "https://example.invalid/1", "zh/weapon/1.json")
        with patch.object(nanoka, "_get_bytes", side_effect=urllib.error.HTTPError(plan.url, 403, "denied", {}, None)) as get:
            with self.assertRaises(RuntimeError):
                nanoka._run_plans([plan, plan], "3.7", {}, nanoka.MirrorReport(), 1.5, False, 10, None)
        self.assertEqual(get.call_count, 1)

    def test_invalid_response_does_not_overwrite_local_file(self):
        plan = nanoka.FilePlan("weapon_detail", "1", "https://example.invalid/1", "zh/weapon/1.json")
        for body in (b'<html>Error</html>', b'{"id":2}'):
            with patch.object(nanoka, "_get_bytes", return_value=(body, {})), \
                 patch.object(nanoka, "_write_atomic") as write, patch.object(nanoka, "_pause"):
                report = nanoka.MirrorReport()
                nanoka._run_plans([plan], "3.7", {}, report, 1.5, False, 10, None)
            write.assert_not_called()
            self.assertEqual(len(report.failed), 1)

    def test_weapon_and_echo_matrices_have_different_orientations(self):
        norm = normalize.Normalized()
        equipment.normalize_weapon({"id": 1, "effect": "{0}/{1}", "param": [["a1", "a2"], ["b1", "b2"]]}, norm)
        equipment.normalize_echo({"id": 2, "skill": {"desc": "{0}/{1}", "param": [["a1", "b1"], ["a2", "b2"]]}}, norm)
        self.assertEqual([r["desc"] for r in norm.tables["weapon_effects"]], ["a1/b1", "a2/b2"])
        self.assertEqual([r["desc"] for r in norm.tables["echo_skills"]], ["a1/b1", "a2/b2"])


@unittest.skipUnless(fixture_available(), f"缺少 fixtures: {FIXTURES}")
class TestPipeline(unittest.TestCase):
    """fixture -> 建库 -> 工具查询。这是整条链路的可回归契约。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="kb-test-")
        cls._orig_raw = config.RAW_DIR
        cls._orig_db = config.DB_PATH
        shutil.copytree(FIXTURES, os.path.join(cls.tmp, "raw"))
        config.RAW_DIR = os.path.join(cls.tmp, "raw")
        config.DB_PATH = os.path.join(cls.tmp, "knowledge.db")
        tools.close()
        cls.report = kb_build.build(version="3.7", locale="zh", db_path=config.DB_PATH)

    @classmethod
    def tearDownClass(cls):
        tools.close()
        config.RAW_DIR = cls._orig_raw
        config.DB_PATH = cls._orig_db
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_schema_created(self):
        conn = tools.connect()
        tables = set(db.all_tables(conn))
        for t in ("characters", "character_stats", "aliases", "refs", "text_index"):
            self.assertIn(t, tables)

    def test_characters_loaded(self):
        conn = tools.connect()
        n = conn.execute("SELECT count(*) FROM characters").fetchone()[0]
        self.assertGreater(n, 0)

    def test_every_character_has_alias(self):
        conn = tools.connect()
        orphans = conn.execute(
            "SELECT count(*) FROM characters c WHERE NOT EXISTS "
            "(SELECT 1 FROM aliases a WHERE a.entity_type='character' "
            " AND a.entity_id = CAST(c.id AS TEXT))"
        ).fetchone()[0]
        self.assertEqual(orphans, 0)

    def test_refs_extracted_from_markup(self):
        conn = tools.connect()
        n = conn.execute("SELECT count(*) FROM refs").fetchone()[0]
        self.assertGreater(n, 0, "fixture 里有 <te href>,应当抽出边")

    def test_resolve_by_chinese_name(self):
        conn = tools.connect()
        char = tools.resolve_character(conn, "爱弥斯")
        self.assertIsNotNone(char)
        self.assertEqual(char["name"], "爱弥斯")

    def test_resolve_by_english_alias(self):
        conn = tools.connect()
        char = tools.resolve_character(conn, "Sanhua")
        self.assertIsNotNone(char, "英文别名应当能解析")

    def test_stats_tool_returns_numbers(self):
        data = json.loads(tools.execute_tool("get_character_stats", {"name": "爱弥斯"}))
        self.assertNotIn("error", data, data)
        self.assertTrue(data["stats"])
        self.assertIsInstance(data["stats"][0]["life"], (int, float))
        self.assertNotIsInstance(data["stats"][0]["life"], str)

    def test_unknown_character_degrades_not_raises(self):
        data = json.loads(tools.execute_tool("get_character_stats", {"name": "不存在xyz"}))
        self.assertIn("error", data)

    def test_unknown_tool_degrades(self):
        data = json.loads(tools.execute_tool("nope", {}))
        self.assertIn("error", data)

    def test_short_chinese_query_falls_back_to_like(self):
        # 回归守卫:trigram 对 2 字查询失效,必须自动退回 LIKE 而不是返回空
        data = json.loads(tools.execute_tool("search_knowledge_text", {"query": "今州"}))
        self.assertEqual(data["route"], "like-fallback", data)
        self.assertTrue(data["results"], "2 字查询不应返回空")

    def test_missing_kb_degrades_gracefully(self):
        tools.close()
        orig = config.DB_PATH
        config.DB_PATH = os.path.join(self.tmp, "does-not-exist.db")
        try:
            data = json.loads(tools.execute_tool("get_character_stats", {"name": "爱弥斯"}))
            self.assertIn("error", data)
            self.assertIn("知识库不存在", data["error"])
        finally:
            config.DB_PATH = orig
            tools.close()

    def test_skill_levels_costs_and_originals_match_source(self):
        with open(os.path.join(FIXTURES, "3.7", "zh", "character", "1102.json"), encoding="utf-8") as fh:
            source = json.load(fh)
        result = json.loads(tools.execute_tool("get_character_skills", {"name": "Sanhua", "tree_id": "1", "level": 2}))
        self.assertNotIn("error", result, result)
        self.assertEqual(result["skills"][0]["attributes"][0]["values"][0], source["skill_trees"]["1"]["skill"]["level"]["1"]["param"][0][1])
        self.assertEqual(result["skills"][0]["upgrade_materials"][0]["amount"], source["skill_trees"]["1"]["skill"]["consume"]["2"][0]["value"])
        self.assertEqual(result["chains"], [])
        chain = json.loads(tools.execute_tool("get_character_skills", {"name": "Sanhua", "chain_no": 1}))
        self.assertIn("15%", chain["chains"][0]["desc"])
        original = tools.connect().execute("SELECT raw_text, params FROM text_sources WHERE entity_type='character' AND entity_id='1102' AND field='chain.1'").fetchone()
        self.assertEqual(original["raw_text"], source["chains"]["1"]["desc"])
        self.assertEqual(json.loads(original["params"]), source["chains"]["1"]["param"])

    def test_skill_catalog_and_pagination_preserve_original(self):
        catalog = tools._get_character_skills("Sanhua")
        self.assertEqual(catalog["view"], "catalog")
        self.assertTrue(all("desc" not in row for row in catalog["skills"] + catalog["chains"]))
        conn = tools.connect()
        row = conn.execute("SELECT c.name, s.tree_id, s.desc FROM character_skills s JOIN characters c ON c.id=s.character_id ORDER BY length(s.desc) DESC LIMIT 1").fetchone()
        pieces, offset = [], 0
        while True:
            result = tools._get_character_skills(row["name"], tree_id=row["tree_id"], offset=offset)
            skill = result["skills"][0]
            self.assertLessEqual(len(skill["desc"]), 5000)
            pieces.append(skill["desc"])
            if skill["next_offset"] is None:
                break
            self.assertGreater(skill["next_offset"], offset)
            offset = skill["next_offset"]
        self.assertEqual("".join(pieces), row["desc"])

    def test_entity_query_by_material_id(self):
        conn = tools.connect()
        row = conn.execute("SELECT id, name FROM items WHERE name<>'' LIMIT 1").fetchone()
        result = json.loads(tools.execute_tool("get_knowledge_entity", {"entity_type": "item", "name": str(row["id"])}))
        self.assertEqual(result["entity"]["name"], row["name"])

    def test_weapon_query_uses_refinement_and_keeps_ascension_boundary(self):
        result = json.loads(tools.execute_tool("get_knowledge_entity", {
            "entity_type": "weapon", "name": "21020015", "level": 20, "refinement": 5}))
        self.assertNotIn("error", result, result)
        entity = result["entity"]
        self.assertTrue(entity["detail_available"])
        self.assertIn("25.6%", entity["effect"])
        self.assertIn("12%", entity["effect"])
        self.assertEqual({r["phase"] for r in entity["stats"]}, {0, 1})
        attack = [r["raw_value"] for r in entity["stats"] if not r["is_ratio"] and not r["is_percent"]]
        self.assertGreater(attack[1], attack[0])
        self.assertTrue(entity["ascensions"])
        self.assertTrue(result["source_url"].endswith("/zh/weapon/21020015.json"))

    def test_echo_query_uses_skill_rank_and_joins_sonatas(self):
        result = json.loads(tools.execute_tool("get_knowledge_entity", {
            "entity_type": "echo", "name": "390070051", "skill_rank": 5}))
        self.assertNotIn("error", result, result)
        entity = result["entity"]
        self.assertTrue(entity["detail_available"])
        self.assertIn("32.00%+64", entity["skill"])
        self.assertNotIn("{0}", entity["skill"])
        self.assertEqual(len(entity["sonatas"]), 3)
        self.assertTrue(all(g["effects"] for g in entity["sonatas"]))
        self.assertTrue(result["source_url"].endswith("/zh/echo/390070051.json"))

    def test_equipment_raw_docs_are_language_scoped(self):
        rows = tools.connect().execute("SELECT locale FROM raw_docs WHERE kind IN ('weapon_detail','echo_detail')").fetchall()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r[0] == "zh" for r in rows))

    def test_queries_from_different_threads(self):
        def query(_):
            try:
                return json.loads(tools.execute_tool("get_character_stats", {"name": "Sanhua"}))
            finally:
                tools.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            for result in pool.map(query, range(4)):
                self.assertNotIn("error", result, result)

    def test_failed_build_preserves_published_database(self):
        tools.close()
        with patch.object(kb_build, "_build_fts", side_effect=RuntimeError("injected failure")):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                kb_build.build(version="3.7", db_path=config.DB_PATH)
        self.assertIsNotNone(tools.resolve_character(tools.connect(), "Sanhua"))

    def test_keep_does_not_duplicate_fts(self):
        tools.close()
        first = kb_build.build(version="3.7", db_path=config.DB_PATH, reset=False)
        second = kb_build.build(version="3.7", db_path=config.DB_PATH, reset=False)
        self.assertEqual(first.counts.get("fts_text"), second.counts.get("fts_text"))

    def test_locale_filter_does_not_mix_character_details(self):
        original = kb_build.nanoka.load_manifest("3.7")
        original["files"]["en/character/1102.json"] = {"kind": "character", "doc_id": "1102"}
        with patch.object(kb_build.nanoka, "load_manifest", return_value=original):
            report = kb_build.build(version="3.7", locale="zh", db_path=os.path.join(self.tmp, "locale.db"))
        self.assertNotIn("en/character/1102.json", report.fields)

    def test_corrupt_mirror_is_rejected(self):
        original = kb_build.nanoka.load_manifest("3.7")
        original["files"]["character.json"]["sha256"] = "invalid"
        with patch.object(kb_build.nanoka, "load_manifest", return_value=original):
            with self.assertRaisesRegex(RuntimeError, "镜像校验失败"):
                kb_build.build(version="3.7", db_path=config.DB_PATH)


if __name__ == "__main__":
    unittest.main(verbosity=2)
