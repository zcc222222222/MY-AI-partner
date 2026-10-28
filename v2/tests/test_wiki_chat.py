"""Chat integration uses isolated data and fake model streams; no network calls."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from llmwiki import WikiStore
from llmwiki import tools as wiki
from app.agents import tools, orchestrator
from app.services import chat_service
from wiki_test_helpers import publish_reviewed


class WikiChatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wiki-chat-")
        self.root = Path(self.tmp.name)
        self.kb = self.root / "knowledge.db"
        self.path = self.root / "wiki.db"
        with closing(sqlite3.connect(self.kb)) as conn, conn:
            conn.executescript("CREATE TABLE meta(key,value); INSERT INTO meta VALUES('game_version','3.7'); "
                               "CREATE TABLE characters(id INTEGER,name TEXT); INSERT INTO characters VALUES(1,'Alpha'),(2,'Beta'); "
                               "CREATE TABLE aliases(entity_type,entity_id,alias); "
                               "INSERT INTO aliases VALUES('character','1','Alias'),('character','1','Twin'),('character','2','Twin');")
        self.store = WikiStore(self.path)
        self.scope = {"game_version": "3.7", "locale": "zh"}
        self.store.import_source("guide", **self.scope, title="guide", raw=b"Limit is 200.", origin="https://example.invalid/source",
                                 metadata={"entities": ["character:1"]})
        draft = {"page_id": "alpha/mechanics", **self.scope, "title": "Alpha mechanics", "kind": "character_mechanics",
                 "body": "Limit is 200. [source:guide@1]", "entities": ["character:1"],
                 "sources": [{"source_id": "guide", "revision": 1}], "claims": [{"claim_id": "cap", "text": "Limit is 200.",
                    "citations": [{"source_id": "guide", "revision": 1, "quote": "Limit is 200."}]}]}
        self.draft_id = self.store.stage_page(draft)["draft_id"]
        publish_reviewed(self.store, self.draft_id)
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("KNOWLEDGE_PATH", self.kb), ("WIKI_PATH", self.path)):
            patcher = patch.object(wiki, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_registered_only_search_not_administrative_commands(self):
        names = [t["function"]["name"] for t in tools.TOOLS]
        self.assertEqual(names.count("search_wiki"), 1)
        self.assertNotIn("publish", names)
        self.assertNotIn("review", names)
        result = json.loads(tools.execute_tool("search_wiki", '{"query":"Limit","entity_name":"Alias"}'))
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["entity"]["id"], "1")
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), 8000)
        self.assertNotIn('"quote"', json.dumps(result))

    def test_entity_and_version_are_strictly_scoped(self):
        self.assertEqual(wiki.search_wiki("Limit", "Beta")["results"], [])
        result = wiki.search_wiki("Limit", "Alpha", game_version="3.8")
        self.assertEqual(result["results"], [])
        self.assertEqual(result["structured_kb_version"], "3.7")
        self.assertIn("version_note", result)
        self.assertIn("不提供", result["version_note"])

    def test_chat_tool_opens_read_only_even_while_update_holds_writer_lock(self):
        connect = sqlite3.connect
        opened = []

        def checked(*args, **kwargs):
            if str(args[0]).startswith(self.path.as_uri()):
                opened.append(str(args[0]))
                self.assertIn('mode=ro', str(args[0]))
            return connect(*args, **kwargs)

        with self.store.connection(write=True):
            with patch('sqlite3.connect', side_effect=checked):
                result = wiki.search_wiki('Limit', 'Alpha')
        self.assertEqual(result['status'], 'found')
        self.assertTrue(opened)

    def test_ambiguous_or_missing_entity_is_not_guessed(self):
        for name in ("Twin", "Missing"):
            self.assertEqual(wiki.search_wiki("Limit", name)["error"], "entity_unresolved")

    def test_missing_wiki_does_not_create_a_database_or_leak_paths(self):
        missing = self.root / "absent/wiki.db"
        with patch.object(wiki, "WIKI_PATH", missing):
            result = wiki.search_wiki("Limit", "Alpha")
        self.assertEqual(result["error"], "wiki_unavailable")
        self.assertFalse(missing.exists())
        self.assertNotIn(str(self.root), json.dumps(result))
        empty = self.root / "empty.db"
        empty.touch()
        with patch.object(wiki, "WIKI_PATH", empty):
            self.assertEqual(wiki.search_wiki("Limit", "Alpha")["error"], "wiki_unavailable")
        self.assertEqual(empty.stat().st_size, 0)

    def test_stale_pages_and_rejected_reviews_are_not_served(self):
        self.store.review(self.draft_id, decision="rejected", reviewer="checker", note="Revise")
        self.assertEqual(wiki.search_wiki("Limit", "Alpha")["results"], [])

    def test_legacy_published_page_without_review_is_excluded(self):
        with self.store.connection(write=True) as conn:
            data = json.loads(conn.execute("SELECT payload FROM page_revisions").fetchone()[0])
            data.update(page_id="beta/mechanics", entities=["character:2"])
            conn.execute("INSERT INTO pages VALUES('beta/mechanics','3.7','zh',1,0,'[]')")
            conn.execute("INSERT INTO page_revisions VALUES('beta/mechanics','3.7','zh',1,?,'legacy','now')", (json.dumps(data),))
            self.store._index(conn, "beta/mechanics", ("3.7", "zh"), 1, data)
            conn.execute("INSERT INTO page_tags VALUES('beta/mechanics','3.7','zh','entities','character:2')")
        self.assertTrue(self.store.search("Limit", **self.scope, entities=["character:2"]))
        self.assertEqual(wiki.search_wiki("Limit", "Beta")["results"], [])

    def test_model_cannot_choose_database_paths(self):
        result = json.loads(tools.execute_tool("search_wiki", json.dumps({"query": "Limit", "entity_name": "Alpha", "wiki_path": "other.db"})))
        self.assertIn("error", result)

    def test_file_origins_are_hidden_from_model(self):
        self.store.import_source("guide", **self.scope, title="guide", raw=b"Limit is 200.", origin=str(self.root / "private.md"), metadata={"entities": ["character:1"]})
        # Updating provenance invalidates the page; republish a freshly pinned draft.
        draft = self.store.get_page("alpha/mechanics", allow_stale=True, **self.scope)["page"]
        draft["base_revision"] = 1
        draft["sources"][0]["revision"] = 2
        for citation in draft["citations"].values():
            citation["revision"] = 2
        draft["body"] = draft["body"].replace("guide@1", "guide@2")
        publish_reviewed(self.store, self.store.stage_page(draft)["draft_id"])
        result = wiki.search_wiki("Limit", "Alpha")
        self.assertEqual(result["status"], "found")
        self.assertIsNone(result["results"][0]["source_refs"][0]["origin"])
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_tool_result_appended_at_tail_and_system_is_unchanged(self):
        messages = [{"role": "system", "content": "byte-stable-persona"}, {"role": "user", "content": "Limit?"}]
        snapshots = []
        calls = [{"id": "wiki-one", "type": "function", "function": {"name": "search_wiki", "arguments": '{"query":"Limit","entity_name":"Alpha"}'}}]
        batches = [[{"type": "tool_calls", "tool_calls": calls}, {"type": "completion", "finish_reason": "tool_calls"}],
                   [{"type": "delta", "content": "Limit is 200."}, {"type": "completion", "finish_reason": "stop"}]]
        def stream(convo, **kwargs):
            snapshots.append(list(convo))
            return iter(batches[len(snapshots) - 1])
        with patch.object(orchestrator, "stream_completion", side_effect=stream):
            events = list(orchestrator.run_agent(messages, tools=tools.TOOLS))
        self.assertEqual(len(messages), 2)
        self.assertEqual(snapshots[1][0], messages[0])
        self.assertEqual(snapshots[1][-1]["role"], "tool")
        self.assertEqual(json.loads(snapshots[1][-1]["content"])["results"][0]["matches"][0]["text"], "Limit is 200.")
        trace = next(e for e in events if e["type"] == "knowledge_trace")
        self.assertEqual(trace["sources"][0]["url"], "https://example.invalid/source")

    def test_source_footer_is_streamed_and_persisted(self):
        self._assert_footer(fallback=False)

    def test_specified_version_cannot_fall_back_to_current_sql_or_default_wiki(self):
        snapshots = []
        calls = []
        for index, (name, arguments) in enumerate([
            ("search_wiki", {"query": "Limit", "entity_name": "Alpha", "game_version": "3.8"}),
            ("search_wiki", {"query": "200", "entity_name": "Alpha"}),
            ("get_character_skills", {"name": "Alpha"})]):
            calls.append({"id": str(index), "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}})
        batches = [[{"type": "tool_calls", "tool_calls": calls}, {"type": "completion", "finish_reason": "tool_calls"}],
                   [{"type": "delta", "content": "No data for 3.8."}, {"type": "completion", "finish_reason": "stop"}]]
        def stream(convo, **kwargs):
            snapshots.append(list(convo))
            return iter(batches[len(snapshots) - 1])
        with patch.object(orchestrator, "stream_completion", side_effect=stream):
            events = list(orchestrator.run_agent([], tools=tools.TOOLS))
        returned = [json.loads(m["content"]) for m in snapshots[1] if m["role"] == "tool"]
        self.assertEqual(returned[0]["game_version"], "3.8")
        self.assertEqual(returned[1]["game_version"], "3.8")
        self.assertEqual(returned[1]["results"], [])
        self.assertEqual(returned[2]["error"], "knowledge_version_mismatch")
        self.assertFalse(any(e.get("sources") for e in events))

    def test_skill_section_keeps_both_forms_ahead_of_incidental_mentions(self):
        raw = "Wing Alpha requires 200. Wing Beta requires 200. Incidental Wing."
        self.store.import_source("guide", **self.scope, title="guide", raw=raw.encode(),
                                 origin="https://example.invalid/source", metadata={"entities": ["character:1"]})
        claims, lines = [], ["## Other topic", ""]
        for index in range(7):
            if index == 5:
                lines.extend(["## Wing conditions", ""])
            text = "Incidental Wing." if index < 5 else ("Wing Alpha requires 200." if index == 5 else "Wing Beta requires 200.")
            lines.append("- " + text + " [source:guide@2]")
            claims.append({"claim_id": str(index), "text": text, "citations": [{"source_id": "guide", "revision": 2, "quote": raw}]})
        draft = {"page_id": "alpha/forms", **self.scope, "title": "Forms", "entities": ["character:1"],
                 "body": "\n".join(lines), "claims": claims, "sources": [{"source_id": "guide", "revision": 2}]}
        publish_reviewed(self.store, self.store.stage_page(draft)["draft_id"])
        result = wiki.search_wiki("Wing", "Alpha")
        text = result["results"][0]["snippet"]
        self.assertIn("Wing Alpha", text)
        self.assertIn("Wing Beta", text)

    def test_budget_fallback_has_no_misleading_source_footer(self):
        self._assert_footer(fallback=True)

    def _assert_footer(self, fallback):
        def events(*args, **kwargs):
            yield {"type": "knowledge_trace", "sources": [{"title": "Alpha", "game_version": "3.7", "url": "https://example.invalid/source"}]}
            yield {"type": "delta", "content": "Answer"}
            if fallback:
                raise orchestrator.IncompleteCompletionError("length", reason="length")
        history = [NS(id=1, role="user", content="question")]
        with patch.object(chat_service, "run_agent", side_effect=events), \
             patch.object(chat_service, "build_system_prompt", return_value="stable"), \
             patch.object(chat_service.session_service, "list_messages", return_value=history), \
             patch.object(chat_service.session_service, "generate_title", return_value="Title"), \
             patch.object(chat_service.session_service, "append_message", return_value=NS(id=7)) as append:
            output = [json.loads(e) for e in chat_service.stream_reply(NS(commit=lambda: None, rollback=lambda: None), NS(id="test", owner_id=1, title=""), NS(), "question")]
        answer = append.call_args.args[3]
        if fallback:
            self.assertEqual(answer, chat_service.BUDGET_FALLBACK)
        else:
            self.assertIn("https://example.invalid/source", answer)
            self.assertTrue(any("资料来源" in e.get("content", "") for e in output))


if __name__ == "__main__":
    unittest.main()
