import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from llmwiki import WikiStore
from llmwiki.compiler import compile_page
from llmwiki.reviewer import review_draft


class ReviewerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = WikiStore(self.root / "wiki.db")
        self.store.import_source("guide", game_version="test", title="Guide", raw=b"When marked, limit is 100.", metadata={"entities": ["character:1"]})
        output = {"sections": [{"heading": "Limit", "claims": [{"text": "Limit is 100.", "evidence_ids": ["E001"]}]}], "open_questions": [], "unresolved_conflicts": []}
        result = compile_page(self.store, "test", game_version="test", entities=["character:1"], transport=lambda *_: self.response(output))
        self.draft = result["draft_id"]

    def tearDown(self):
        self.tmp.cleanup()

    def response(self, value, **kwargs):
        return {"content": json.dumps(value), "finish_reason": "stop", "usage": {"completion_tokens": 100}, **kwargs}

    def test_independent_review_covers_every_claim_before_approval(self):
        result = review_draft(self.store, self.draft, run_dir=self.root / "review", transport=lambda *_: self.response({"checked_claim_ids": ["claim-001"], "findings": []}))
        self.assertEqual(result["status"], "approved")
        self.assertEqual(self.store.search("Limit", game_version="test"), [])
        self.store.publish(self.draft)
        self.assertTrue(self.store.search("Limit", game_version="test", reviewed_only=True))

    def test_rewrite_requires_second_complete_review_and_keeps_evidence(self):
        replies = [{"checked_claim_ids": ["claim-001"], "findings": [{"claim_id": "claim-001", "replacement": "When marked, limit is 100.", "reason": "Missing marked prerequisite"}]}, {"checked_claim_ids": ["claim-001"], "findings": []}]
        mock = Mock(side_effect=[self.response(x) for x in replies])
        result = review_draft(self.store, self.draft, run_dir=self.root / "review", transport=mock)
        self.assertEqual(result["status"], "approved")
        self.assertNotEqual(result["draft_id"], self.draft)
        self.store.publish(result["draft_id"])
        self.assertIn("When marked", self.store.get_page("test", game_version="test")["page"]["body"])

    def test_partial_truncated_or_unfixable_review_fails_closed(self):
        outputs = [{"checked_claim_ids": [], "findings": []}, {"checked_claim_ids": ["claim-001"], "findings": [{"claim_id": "claim-001", "replacement": None, "reason": "unsupported"}]}]
        for index, output in enumerate(outputs):
            result = review_draft(self.store, self.draft, run_dir=self.root / str(index), transport=lambda *_, x=output: self.response(x))
            self.assertEqual(result["status"], "needs_attention")
        result = review_draft(self.store, self.draft, run_dir=self.root / "short", transport=lambda *_: self.response(outputs[0], finish_reason="length"))
        self.assertEqual(result["status"], "needs_attention")
