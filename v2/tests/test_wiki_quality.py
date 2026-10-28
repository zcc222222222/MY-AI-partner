"""Regression tests for review boundaries, compact citations and upstream freshness."""
import json
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from llmwiki import WikiStore, WikiError
from llmwiki.compiler import compile_page, SYSTEM_PROMPT
from llmwiki.ingest import sync_nanoka
from llmwiki.payload import citations_for
from llmwiki.evidence import build_evidence
from wiki_test_helpers import publish_reviewed


class WikiQualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wiki-quality-")
        self.root = Path(self.tmp.name)
        self.store = WikiStore(self.root / "wiki.db")
        self.scope = {"game_version": "3.7", "locale": "zh"}
        self.store.import_source("guide", **self.scope, title="guide", raw=b"Limit is 200.", metadata={"entities": ["character:1"]})

    def tearDown(self):
        self.tmp.cleanup()

    def reply(self, text="Limit is 200."):
        return {"finish_reason": "stop", "model": "offline", "usage": {"completion_tokens": 100}, "content": json.dumps({
            "sections": [{"heading": "Limit", "claims": [{"text": text, "evidence_ids": ["E001"]}]}],
            "open_questions": [], "unresolved_conflicts": []})}

    def compile(self, transport=None, **kwargs):
        return compile_page(self.store, "page", **self.scope, entities=["character:1"],
                            transport=transport or Mock(return_value=self.reply()), **kwargs)

    def test_wrong_number_with_correct_quote_cannot_pass_review(self):
        result = self.compile(Mock(return_value=self.reply("Limit is 999.")))
        self.assertFalse(result["valid"])
        self.assertTrue(any("unsupported_numbers" in e for e in result["errors"]))
        with self.assertRaises(WikiError):
            self.store.review(result["draft_id"], decision="approved", reviewer="agent", note="Checked")

    def test_publish_requires_explicit_review_and_rejection_revokes_approval(self):
        result = self.compile(publish=True)
        self.assertEqual(result["status"], "awaiting_review")
        self.assertFalse(result["published"])
        with self.assertRaisesRegex(WikiError, "review_required"):
            self.store.publish(result["draft_id"])
        self.store.review(result["draft_id"], decision="approved", reviewer="a", note="Checked")
        self.store.review(result["draft_id"], decision="rejected", reviewer="b", note="Needs revision")
        with self.assertRaisesRegex(WikiError, "review_required"):
            self.store.publish(result["draft_id"])
        self.assertTrue(publish_reviewed(self.store, result["draft_id"])["changed"])

    def test_same_inputs_reuse_pending_draft_and_skip_published_page(self):
        transport = Mock(return_value=self.reply())
        first = self.compile(transport)
        second = self.compile(transport)
        self.assertEqual(second["draft_id"], first["draft_id"])
        publish_reviewed(self.store, first["draft_id"])
        third = self.compile(transport)
        self.assertEqual(third["status"], "skipped")
        self.assertEqual(transport.call_count, 1)
        with patch("llmwiki.compiler.SYSTEM_PROMPT", SYSTEM_PROMPT + "\nChanged recipe."):
            fourth = self.compile(transport)
        self.assertEqual(transport.call_count, 2)
        self.assertNotEqual(first["input_fingerprint"], fourth["input_fingerprint"])

    def test_citations_stored_once_and_search_does_not_return_long_quotes(self):
        output = json.loads(self.reply()["content"])
        output["sections"][0]["claims"] *= 20
        result = self.compile(Mock(return_value={**self.reply(), "content": json.dumps(output)}))
        publish_reviewed(self.store, result["draft_id"])
        page = self.store.get_page("page", **self.scope)["page"]
        self.assertEqual(len(page["citations"]), 1)
        self.assertEqual(len(page["claims"]), 20)
        self.assertNotIn("citations", page["claims"][0])
        self.assertEqual(citations_for(page, page["claims"][0])[0]["quote"], "Limit is 200.")
        hits = self.store.search("200", **self.scope)
        self.assertIn("200", hits[0]["snippet"])
        self.assertNotIn('"quote"', json.dumps(hits))
        self.assertTrue(hits[0]["source_refs"])
        self.assertLessEqual(len(json.dumps(self.store.search("200", max_chars=1000, **self.scope), ensure_ascii=False)), 1000)

    def test_long_document_tail_is_retrieved_instead_of_prefix(self):
        long = "Introduction " * 200 + "Tail mechanism needs 200 energy."
        self.store.import_source("guide", **self.scope, title="guide", raw=long.encode(), metadata={"entities": ["character:1"]})
        result = self.compile(Mock(return_value=self.reply("Tail mechanism needs 200 energy.")))
        publish_reviewed(self.store, result["draft_id"])
        hits = self.store.search("Tail mechanism", **self.scope)
        self.assertIn("Tail mechanism", hits[0]["snippet"])
        self.assertNotIn("Introduction", hits[0]["snippet"])

    def test_subscription_refresh_hides_upstream_changes_without_manual_sync(self):
        path = self.root / "knowledge.db"
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE raw_docs(kind,doc_id,game_version,locale,payload,sha256,source_url)")
            raw = json.dumps({"skill_trees": {"7": {"skill": {"name": "Limit", "desc": "Limit is 200."}}}})
            conn.execute("INSERT INTO raw_docs VALUES(?,?,?,?,?,?,?)", ("character", "2", "3.7", "zh", raw, hashlib.sha256(raw.encode()).hexdigest(), "https://example.invalid/source"))
        sync_nanoka(self.store, kind="character", doc_id="2", knowledge_path=path, **self.scope)
        result = compile_page(self.store, "two", **self.scope, entities=["character:2"], transport=Mock(return_value=self.reply()))
        publish_reviewed(self.store, result["draft_id"])
        with closing(sqlite3.connect(path)) as conn, conn:
            changed = raw.replace("200", "300")
            conn.execute("UPDATE raw_docs SET payload=?,sha256=?", (changed, hashlib.sha256(changed.encode()).hexdigest()))
        self.assertEqual(self.store.search("200", **self.scope), [])
        self.assertEqual(self.store.plan(**self.scope)["compile_order"], ["two"])
        with self.assertRaises(WikiError):
            self.store.get_page("two", **self.scope)
        path.rename(self.root / "unavailable.db")
        with self.assertRaisesRegex(WikiError, "upstream_unavailable"):
            self.store.search("200", **self.scope)

    def test_unimplemented_derived_kind_fails_before_model_call(self):
        transport = Mock()
        result = self.compile(transport, kind="version_digest")
        self.assertFalse(result["valid"])
        transport.assert_not_called()

    def test_legacy_database_upgrade_preserves_immutable_history(self):
        with self.store.connection(write=True) as conn:
            conn.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
        upgraded = WikiStore(self.store.path)
        with upgraded.connection() as conn:
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "2")
            self.assertEqual(conn.execute("SELECT count(*) FROM source_revisions").fetchone()[0], 1)

    def test_rejecting_a_published_draft_withdraws_it_from_search(self):
        result = self.compile()
        publish_reviewed(self.store, result["draft_id"])
        self.store.review(result["draft_id"], decision="rejected", reviewer="reviewer", note="Condition needs correction")
        self.assertEqual(self.store.search("Limit", **self.scope), [])
        self.assertEqual(self.store.index_jobs()[-1]["operation"], "withdraw")

    def test_rejected_draft_is_not_reused_by_compile_cache(self):
        transport = Mock(return_value=self.reply())
        first = self.compile(transport)
        self.store.review(first["draft_id"], decision="rejected", reviewer="reviewer", note="Revise wording")
        second = self.compile(transport)
        self.assertNotEqual(first["draft_id"], second["draft_id"])
        self.assertEqual(transport.call_count, 2)

    def test_citation_path_cannot_claim_to_point_to_another_json_field(self):
        self.store.import_source("json", **self.scope, title="json", raw=b'{"a":"Limit is 200.","b":"Limit is 300."}', media_type="application/json")
        draft = {"page_id": "badpath", **self.scope, "title": "test", "body": "Limit is 200. [source:json@1]",
                 "sources": [{"source_id": "json", "revision": 1}],
                 "claims": [{"claim_id": "c", "text": "Limit is 200.", "citations": [{"source_id": "json", "revision": 1, "quote": '"Limit is 200."', "source_path": ["b"]}]}]}
        staged = self.store.stage_page(draft)
        self.assertFalse(staged["valid"])
        self.assertTrue(any("invalid_source_path" in e for e in staged["errors"]))

    def test_overview_and_story_use_separate_evidence_views(self):
        data = {"name": "Example", "desc": "A performer.", "chara_info": {"country": "Example city"},
                "skill_trees": {"7": {"skill": {"name": "Skill", "desc": "Limit is 200."}}},
                "stories": {"1": {"title": "Story", "desc": "A journey."}},
                "voices": {"1": {"desc": "A greeting."}}}
        self.store.import_source("nanoka/character/3", **self.scope, title="example", raw=json.dumps(data).encode(),
                                 media_type="application/json", metadata={"adapter": "nanoka_raw_docs", "entities": ["character:3"]})
        overview, _ = build_evidence(self.store, self.store.prepare("overview", **self.scope, entities=["character:3"], kind="character_overview"))
        story, _ = build_evidence(self.store, self.store.prepare("story", **self.scope, entities=["character:3"], kind="story_digest"))
        self.assertTrue(any(e["source_path"][0] == "chara_info" for e in overview))
        self.assertFalse(any(e["source_path"][0] in ("stories", "voices") for e in overview))
        self.assertTrue(all(e["source_path"][0] in ("stories", "voices", "goods") for e in story))

    def test_glossary_selects_one_term_instead_of_entire_dictionary(self):
        self.store.import_source("terms", **self.scope, title="terms", raw=b'{"1":{"name":"One","desc":"First term."},"2":{"name":"Two","desc":"Other term."}}',
                                 media_type="application/json", metadata={"entities": ["hyperlink:1", "hyperlink:2"]})
        evidence, _ = build_evidence(self.store, self.store.prepare("term-one", **self.scope, entities=["hyperlink:1"], kind="glossary"))
        self.assertTrue(evidence)
        self.assertTrue(all(e["source_path"][0] == "1" for e in evidence))

    def test_skill_heading_is_preserved_when_claim_uses_implicit_subject(self):
        output = json.loads(self.reply()["content"])
        output["sections"][0]["heading"] = "Hidden ability"
        result = self.compile(Mock(return_value={**self.reply(), "content": json.dumps(output)}))
        publish_reviewed(self.store, result["draft_id"])
        hits = self.store.search("Hidden ability", **self.scope)
        self.assertEqual(hits[0]["matches"][0]["heading"], "Hidden ability")
        self.assertIn("200", hits[0]["snippet"])


if __name__ == "__main__":
    unittest.main()
