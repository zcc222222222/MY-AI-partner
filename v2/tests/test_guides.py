"""Curated recommendations must not contaminate existing mechanism pages."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from llmwiki import WikiStore, WikiError
from llmwiki.guides import import_guide
from llmwiki.compiler import compile_page
from llmwiki.evidence import build_evidence
from wiki_test_helpers import publish_reviewed


class GuideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.scope = {"game_version": "3.7"}
        self.store = WikiStore(self.root / "wiki.db")
        self.kb = self.root / "knowledge.db"
        with sqlite3.connect(self.kb) as c:
            c.execute("CREATE TABLE characters(id INTEGER PRIMARY KEY,name TEXT)")
            c.execute("INSERT INTO characters VALUES(1,'角色甲')")
        c.close()
        self.version_quote = "Last review update Patch 3.7 Last major build/calcs update Patch 3.6"
        self.quote = "Alice is a suggested teammate."
        self.snapshot = self.root / "snapshot.txt"
        self.snapshot.write_text(self.version_quote + " " + self.quote + " 2026-10-17", encoding="utf-8")
        self.manifest = self.root / "manifest.json"
        self.data = {"schema": "curated_guide_v1", "title": "Toy guide", "source_url": "https://example.invalid/alice",
            "review_patch": "3.7", "calculation_patch": "3.6", "updated_at": "2026-10-17", "captured_at": "2026-10-17",
            "version_quote": self.version_quote, "snapshot_sha256": hashlib.sha256(self.snapshot.read_bytes()).hexdigest(),
            "entities": [{"type": "character", "id": "1", "name": "角色甲", "source_name": "Alice"}],
            "excerpts": [{"label": "Teammate", "quote": self.quote}]}

    def tearDown(self):
        self.tmp.cleanup()

    def save(self):
        self.manifest.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")

    def ingest(self, **kwargs):
        self.save()
        return import_guide(self.store, self.manifest, self.snapshot, source_id="guide/alice",
            game_version=kwargs.get("game_version", "3.7"), knowledge_path=self.kb)

    def compile(self, pid="guide", kind="team_guide", text=None):
        response = {"finish_reason": "stop", "usage": {"completion_tokens": 50}, "content": json.dumps({
            "sections": [{"heading": "Team", "claims": [{"text": text or "社区来源建议角色甲作为队友；复核3.7、计算基线3.6。", "evidence_ids": ["E001"]}]}],
            "open_questions": [], "unresolved_conflicts": []})}
        return compile_page(self.store, pid, **self.scope, kind=kind, entities=["character:1"], transport=Mock(return_value=response))

    def test_exact_snapshot_entity_mapping_and_idempotent_import(self):
        self.assertTrue(self.ingest()["changed"])
        self.assertFalse(self.ingest()["changed"])
        evidence, _ = build_evidence(self.store, self.store.prepare("guide", **self.scope, kind="team_guide", entities=["character:1"]))
        self.assertIn("复核版本3.7", evidence[0]["text"])
        self.assertIn("计算基线3.6", evidence[0]["text"])
        self.assertIn("角色甲", evidence[0]["text"])

    def test_reject_changed_snapshot_and_fabricated_quote(self):
        self.data["excerpts"][0]["quote"] = "Not in source"
        with self.assertRaises(WikiError): self.ingest()
        self.data["excerpts"][0]["quote"] = self.quote
        self.snapshot.write_text("changed", encoding="utf-8")
        with self.assertRaises(WikiError): self.ingest()

    def test_reject_name_id_mismatch_and_version_relabel(self):
        self.data["entities"][0]["name"] = "其他角色"
        with self.assertRaises(WikiError): self.ingest()
        self.data["entities"][0]["name"] = "角色甲"
        with self.assertRaises(WikiError): self.ingest(game_version="3.8")
        self.data["review_patch"], self.data["calculation_patch"] = "3.6", "3.7"
        with self.assertRaises(WikiError): self.ingest(game_version="3.6")

    def test_guide_does_not_invalidate_or_enter_mechanism_page(self):
        self.store.import_source("native", **self.scope, title="native", raw=b"Plain mechanics.", metadata={"entities": ["character:1"]})
        page = self.compile(pid="mechanics", kind="character_mechanics", text="Plain mechanics.")
        self.assertTrue(page["valid"], page)
        publish_reviewed(self.store, page["draft_id"])
        self.ingest()
        self.assertEqual(self.store.get_page("mechanics", **self.scope)["page"]["kind"], "character_mechanics")
        mech = self.store.prepare("mechanics", **self.scope)
        self.assertEqual([s["source_id"] for s in mech["sources"]], ["native"])
        guide = self.store.prepare("guide", **self.scope, kind="team_guide", entities=["character:1"])
        self.assertEqual([s["source_id"] for s in guide["sources"]], ["guide/alice"])

    def test_guide_requires_review_and_change_withdraws_it(self):
        self.ingest()
        page = self.compile()
        self.assertTrue(page["valid"], page)
        self.assertEqual(self.store.search("角色甲", **self.scope, kind="team_guide", reviewed_only=True), [])
        with self.assertRaises(WikiError): self.store.publish(page["draft_id"])
        publish_reviewed(self.store, page["draft_id"])
        hits = self.store.search("角色甲", **self.scope, kind="team_guide", reviewed_only=True)
        self.assertTrue(hits)
        self.assertIn("scope_note", hits[0])
        self.assertTrue(all(r.get("source_path", [None])[0] == "excerpts" for m in hits[0]["matches"] for r in m["source_refs"]))
        self.assertFalse(self.store.search("角色甲", game_version="3.8", kind="team_guide", reviewed_only=True))
        self.data["title"] = "Updated guide"
        self.ingest()
        self.assertFalse(self.store.search("角色甲", **self.scope, kind="team_guide", reviewed_only=True))

    def test_invented_performance_number_rejected(self):
        self.ingest()
        self.assertFalse(self.compile(text="社区来源建议角色甲；复核3.7、计算基线3.6，提升99%。")["valid"])

    def test_unscoped_source_cannot_enter_guide(self):
        self.store.import_source("unscoped", **self.scope, title="unreviewed", raw=b"Do not use", metadata={"entities": ["character:1"]})
        job = self.store.prepare("guide", **self.scope, kind="team_guide", entities=["character:1"])
        self.assertEqual(job["sources"], [])


if __name__ == "__main__": unittest.main()
