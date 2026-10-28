"""Compiler tests use fake transports; never spend API tokens in offline tests."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from llmwiki import WikiStore
from llmwiki.payload import citations_for
from wiki_test_helpers import publish_reviewed
from llmwiki.compiler import CompileOptions, compile_page, complete_json, render
from llmwiki.evidence import build_evidence


class CompilerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wiki-compiler-")
        self.store = WikiStore(Path(self.tmp.name) / "wiki.db")
        self.scope = {"game_version": "3.7", "locale": "zh"}
        self.store.import_source("guide", **self.scope, title="资源", raw="上限为200。".encode(),
                                 metadata={"entities": ["character:1"]})

    def tearDown(self):
        self.tmp.cleanup()

    def output(self, **overrides):
        result = {"sections": [{"heading": "资源", "claims": [{"text": "上限为200。", "evidence_ids": ["E001"]}]}],
                  "open_questions": [], "unresolved_conflicts": []}
        return result | overrides

    def reply(self, output=None, **overrides):
        return {"content": json.dumps(self.output() if output is None else output, ensure_ascii=False),
                "finish_reason": "stop", "usage": {"completion_tokens": 100}, "model": "fake", **overrides}

    def compile(self, transport, **kwargs):
        publish = kwargs.pop("publish", False)
        kwargs.setdefault("force", True)
        result = compile_page(self.store, "mechanics", **self.scope, title="mechanics", entities=["character:1"],
                              transport=transport, runs_dir=Path(self.tmp.name) / "runs", **kwargs)
        if publish and result["valid"]:
            publication = publish_reviewed(self.store, result["draft_id"])
            result.update(published=True, publication=publication, status="published")
        return result

    def test_real_rendered_draft_staged_then_published(self):
        transport = Mock(return_value=self.reply(reasoning_content="never persist"))
        report = self.compile(transport)
        self.assertEqual(report["status"], "staged")
        self.assertEqual(self.store.search("上限", **self.scope), [])
        publish_reviewed(self.store, report["draft_id"])
        page = self.store.get_page("mechanics", **self.scope)["page"]
        self.assertEqual(citations_for(page, page["claims"][0])[0]["source_id"], "guide")
        self.assertIn("[source:guide@1]", page["body"])
        self.assertNotIn("never persist", "".join(p.read_text(encoding="utf-8") for p in Path(report["run_dir"]).glob("*.json")))
        self.assertEqual(transport.call_count, 1)

    def test_bad_json_retries_once_and_accounts_usage(self):
        transport = Mock(side_effect=[self.reply(content="{bad"), self.reply()])
        report = self.compile(transport)
        self.assertTrue(report["valid"])
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(len(report["attempts"]), 2)

    def test_truncation_even_of_valid_json_is_rejected(self):
        transport = Mock(return_value=self.reply(finish_reason="length"))
        report = self.compile(transport, publish=True)
        self.assertFalse(report["valid"])
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(self.store.search("上限", **self.scope), [])

    def test_unknown_evidence_cannot_be_published(self):
        output = self.output()
        output["sections"][0]["claims"][0]["evidence_ids"] = ["E999"]
        report = self.compile(Mock(return_value=self.reply(output)), publish=True)
        self.assertFalse(report["valid"])
        self.assertNotIn("draft_id", report)

    def test_no_silent_evidence_omission(self):
        self.store.import_source("extra", **self.scope, title="补充", raw="状态持续5秒。".encode(), metadata={"entities": ["character:1"]})
        report = self.compile(Mock(return_value=self.reply()))
        self.assertFalse(report["valid"])
        self.assertTrue(any("遗漏证据" in e for e in report["errors"]))

    def test_model_cannot_replace_scope_or_source_manifest(self):
        report = self.compile(Mock(return_value=self.reply(self.output(sources=[{"source_id": "fake"}]))))
        self.assertFalse(report["valid"])

    def test_source_changes_during_generation_prevent_publication(self):
        def transport(*args):
            self.store.import_source("guide", **self.scope, title="资源", raw="上限为300。".encode(), metadata={"entities": ["character:1"]})
            return self.reply()
        report = self.compile(transport, publish=True)
        self.assertEqual(report["status"], "rejected")
        self.assertTrue(any("stale_source" in e for e in report["errors"]))
        self.assertEqual(self.store.search("上限", **self.scope), [])

    def test_generation_failure_preserves_published_revision(self):
        first = self.compile(Mock(return_value=self.reply()), publish=True)
        self.assertTrue(first["published"])
        report = self.compile(Mock(return_value=self.reply(content="")), publish=True)
        self.assertFalse(report["published"])
        self.assertEqual(self.store.get_page("mechanics", **self.scope)["revision"], 1)

    def test_changed_source_invalidates_then_recompiles_with_new_revision(self):
        self.assertTrue(self.compile(Mock(return_value=self.reply()), publish=True)["published"])
        self.store.import_source("guide", **self.scope, title="资源", raw="上限为300。".encode(),
                                 metadata={"entities": ["character:1"]})
        self.assertEqual(self.store.search("上限", **self.scope), [])
        output = self.output()
        output["sections"][0]["claims"][0]["text"] = "上限为300。"
        report = self.compile(Mock(return_value=self.reply(output)), publish=True)
        self.assertTrue(report["published"])
        page = self.store.get_page("mechanics", **self.scope)
        self.assertEqual(page["revision"], 2)
        self.assertEqual(citations_for(page["page"], page["page"]["claims"][0])[0]["revision"], 2)
        self.assertEqual(self.store.search("300", **self.scope)[0]["revision"], 2)
        history = self.store.get_page("mechanics", revision=1, **self.scope)
        self.assertIn("200", history["page"]["body"])

    def test_input_budget_rejects_before_api_and_does_not_clip_sources(self):
        transport = Mock()
        report = self.compile(transport, options=CompileOptions(max_input_chars=1))
        self.assertFalse(report["valid"])
        transport.assert_not_called()

    def test_output_budget_prevents_second_request(self):
        transport = Mock(return_value=self.reply(content="{bad", usage={"completion_tokens": 8192}))
        report = self.compile(transport, options=CompileOptions(total_output_tokens=8192))
        self.assertFalse(report["valid"])
        self.assertEqual(transport.call_count, 1)

    def test_reported_conflicts_remain_unpublished(self):
        report = self.compile(Mock(return_value=self.reply(self.output(unresolved_conflicts=["来源互相矛盾"]))), publish=True)
        self.assertEqual(report["status"], "rejected")
        self.assertFalse(report["published"])

    def test_projection_expands_parameters_and_retains_exact_anchors(self):
        data = {"skill_trees": {"7": {"skill": {"name": "示例", "desc": "<color>消耗{0}点，持续{1}秒。</color>", "param": ["100", "5"]}}}}
        self.store.import_source("nanoka/character/7", **self.scope, title="nanoka", raw=json.dumps(data, ensure_ascii=False).encode(),
                                 media_type="application/json", metadata={"entities": ["character:7"], "adapter": "nanoka_raw_docs"})
        job = self.store.prepare("seven", **self.scope, entities=["character:7"])
        evidence, omitted = build_evidence(self.store, job)
        self.assertEqual(evidence[0]["text"], "消耗100点，持续5秒。")
        source = self.store.get_source("nanoka/character/7", **self.scope)
        self.assertTrue(all(c["quote"] in source["text"] for c in evidence[0]["citations"]))
        self.assertEqual(len(evidence[0]["citations"]), 3)
        self.assertTrue(omitted)

    def test_json_transport_disables_thinking_and_has_no_hidden_retries(self):
        response = NS(choices=[NS(finish_reason="stop", message=NS(content="{}"))], usage=None)
        client = Mock()
        client.with_options.return_value.chat.completions.create.return_value = response
        with patch("dotenv.load_dotenv"), patch("app.config.settings", NS(api_key_ready=True, deepseek_model="test")), patch("app.llm._get_client", return_value=client):
            result = complete_json([], 8192)
        self.assertEqual(result["finish_reason"], "stop")
        self.assertEqual(client.with_options.call_args.kwargs["max_retries"], 0)
        kwargs = client.with_options.return_value.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertEqual(kwargs["response_format"], {"type": "json_object"})

    def test_chain_scope_survives_rendering_and_claim_extraction(self):
        job = self.store.prepare("mechanics", **self.scope, entities=["character:1"])
        evidence, _ = build_evidence(self.store, job)
        evidence[0]["source_path"] = ["chains", "4"]
        draft = render(self.output(), job["draft_template"], evidence, CompileOptions())
        self.assertTrue(draft["claims"][0]["text"].startswith("【共鸣链4】"))
        self.assertIn("【共鸣链4】上限为200。", draft["body"])

    def test_different_chains_cannot_be_merged_into_one_claim(self):
        job = self.store.prepare("mechanics", **self.scope, entities=["character:1"])
        evidence, _ = build_evidence(self.store, job)
        evidence[0]["source_path"] = ["chains", "4"]
        evidence.append({**evidence[0], "evidence_id": "E002", "source_path": ["chains", "6"]})
        output = self.output()
        output["sections"][0]["claims"][0]["evidence_ids"].append("E002")
        with self.assertRaisesRegex(ValueError, "共鸣链"):
            render(output, job["draft_template"], evidence, CompileOptions())


if __name__ == "__main__":
    unittest.main()
