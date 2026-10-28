import hashlib
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from llmwiki import WikiStore, WikiError
from llmwiki.roster import plan_roster, compile_roster
from llmwiki.compiler import compile_page
from wiki_test_helpers import publish_reviewed


class RosterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = WikiStore(self.root / "wiki.db")
        self.kb = self.root / "knowledge.db"
        with closing(sqlite3.connect(self.kb)) as conn, conn:
            conn.executescript("CREATE TABLE characters(id INTEGER,name TEXT,game_version TEXT); CREATE TABLE raw_docs(kind TEXT,doc_id TEXT,game_version TEXT,locale TEXT,payload TEXT,sha256 TEXT,source_url TEXT);")
            raw = json.dumps({"skill_trees": {"1": {"skill": {"name": "Resource", "desc": "Limit is 100."}}}})
            for ident in (1, 2, 3):
                conn.execute("INSERT INTO characters VALUES(?, 'Same name', 'test')", (ident,))
                conn.execute("INSERT INTO raw_docs VALUES('character',?,'test','zh',?,?, 'https://example.invalid/source')", (str(ident), raw, hashlib.sha256(raw.encode()).hexdigest()))
        self.scope = dict(game_version="test", knowledge_path=self.kb)
        self.transport = Mock(return_value={"content": json.dumps({"sections": [{"heading": "Resource", "claims": [{"text": "Limit is 100.", "evidence_ids": ["E001"]}]}], "open_questions": [], "unresolved_conflicts": []}), "finish_reason": "stop", "usage": {"completion_tokens": 100}, "model": "fake"})

    def tearDown(self):
        self.tmp.cleanup()

    def compile(self, store, page_id, **kwargs):
        return compile_page(store, page_id, transport=self.transport, **kwargs)

    def batch(self, **kwargs):
        return compile_roster(self.store, **self.scope, report_path=self.root / "batch.json", compile_fn=self.compile, pause=lambda _: None, **kwargs)

    def test_stable_ids_bounded_batch_and_resume_reuses_drafts(self):
        plan = plan_roster(self.store, **self.scope)
        self.assertEqual(plan["total"], 3)
        self.assertEqual(len({e["page_id"] for e in plan["entries"]}), 3)
        result = self.batch(limit=2)
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["status"], "awaiting_review")
        self.assertFalse(result["auto_publish"])
        self.assertEqual(self.transport.call_count, 2)
        self.assertEqual(self.store.search("Limit", game_version="test"), [])
        again = self.batch(limit=2)
        self.assertEqual(self.transport.call_count, 2)
        self.assertTrue(all(r["compile"]["reason"] == "reused_draft" for r in again["results"]))

    def test_existing_published_page_keeps_slug_and_is_skipped(self):
        result = self.batch(limit=1)
        draft = result["results"][0]["compile"]["draft_id"]
        publish_reviewed(self.store, draft)
        plan = plan_roster(self.store, **self.scope)
        self.assertEqual(plan["counts"]["current"], 1)
        self.assertEqual(self.batch(character_ids=[1])["status"], "nothing_to_compile")
        self.assertEqual(self.transport.call_count, 1)

    def test_missing_or_ambiguous_raw_sources_block_compilation(self):
        with closing(sqlite3.connect(self.kb)) as conn, conn:
            conn.execute("DELETE FROM raw_docs WHERE doc_id='1'")
            conn.execute("INSERT INTO raw_docs SELECT kind,doc_id,game_version,'',payload,sha256,source_url FROM raw_docs WHERE doc_id='2'")
        plan = plan_roster(self.store, **self.scope)
        self.assertEqual([e["status"] for e in plan["entries"]], ["missing_source", "ambiguous_source", "missing_page"])
        result = self.batch()
        self.assertEqual([r["character_id"] for r in result["results"]], ["3"])

    def test_invalid_ids_versions_and_limits_rejected_before_model_calls(self):
        for limit in (0, 6):
            with self.assertRaises(WikiError):
                self.batch(limit=limit)
        with self.assertRaises(WikiError):
            self.batch(character_ids=[999])
        with self.assertRaises(WikiError):
            plan_roster(self.store, game_version="other", knowledge_path=self.kb)
        self.transport.assert_not_called()
