"""Offline update lifecycle, stale evidence, publication races and index recovery."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from llmwiki import WikiError, WikiStore
from wiki_test_helpers import publish_reviewed
from llmwiki.ingest import import_file, sync_nanoka

SCOPE = {"game_version": "3.7", "locale": "zh"}


class WikiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wiki-tests-")
        self.store = WikiStore(Path(self.tmp.name) / "wiki.db")
        self.add_source()

    def tearDown(self):
        self.tmp.cleanup()

    def add_source(self, sid="guide", raw="同步率上限为200。", entities=None, **kwargs):
        return self.store.import_source(sid, **(SCOPE | kwargs), title=sid, raw=raw.encode(),
                                        metadata={"entities": entities or [], "topics": ["mechanics"] if entities else []})

    def draft(self, pid="base", sid="guide", rev=1, text="同步率上限为200。", **extra):
        return {"page_id": pid, **SCOPE, "title": "资源机制", "body": text + f" [source:{sid}@{rev}]",
                "sources": [{"source_id": sid, "revision": rev}], "pages": [],
                "claims": [{"claim_id": "cap", "text": text,
                            "citations": [{"source_id": sid, "revision": rev, "quote": text}]}], **extra}

    def publish(self, data=None):
        staged = self.store.stage_page(data or self.draft())
        self.assertTrue(staged["valid"], staged)
        return publish_reviewed(self.store, staged["draft_id"])

    def test_import_idempotence_bytes_and_metadata_revisions(self):
        raw = b"\xef\xbb\xbfhello\r\n"
        one = self.store.import_source("bytes", **SCOPE, title="bytes", raw=raw)
        two = self.store.import_source("bytes", **SCOPE, title="bytes", raw=raw)
        self.assertFalse(two["changed"])
        self.assertEqual(one["revision"], two["revision"])
        three = self.store.import_source("bytes", **SCOPE, title="renamed", raw=raw)
        self.assertEqual(three["revision"], 2)
        self.assertEqual(self.store.get_source("bytes", **SCOPE, revision=1, include_raw=True)["raw"], raw)

    def test_published_content_search_and_idempotent_publish(self):
        stage = self.store.stage_page(self.draft())
        self.assertTrue(stage["valid"])
        self.assertEqual(self.store.search("同步率", **SCOPE), [])
        publish_reviewed(self.store, stage["draft_id"])
        self.assertFalse(publish_reviewed(self.store, stage["draft_id"])["changed"])
        self.assertFalse(self.publish()["changed"])
        self.assertEqual(self.store.get_page("base", **SCOPE)["revision"], 1)
        self.assertEqual(len(self.store.search("同步率", **SCOPE)), 1)
        self.assertEqual(len(self.store.search("资源", **SCOPE)), 1)
        self.assertEqual(self.store.plan(**SCOPE)["uncompiled_sources"], [])

    def test_read_only_search_does_not_initialize_or_write_and_allows_writer(self):
        self.publish()
        statements = []
        connect = sqlite3.connect

        def traced(*args, **kwargs):
            conn = connect(*args, **kwargs)
            if str(args[0]).startswith(self.store.path.as_uri()):
                conn.set_trace_callback(statements.append)
            return conn

        before = self.store.path.read_bytes()
        with self.store.connection(write=True):
            with patch('sqlite3.connect', side_effect=traced):
                reader = WikiStore(self.store.path, read_only=True)
                self.assertEqual(len(reader.search('同步率', reviewed_only=True, **SCOPE)), 1)
                with self.assertRaises(WikiError):
                    reader.import_source('blocked', **SCOPE, title='blocked', raw=b'blocked')
                with reader.connection() as conn:
                    with self.assertRaises(sqlite3.OperationalError):
                        conn.execute("UPDATE meta SET value='999' WHERE key='generation'")
        self.assertEqual(before, self.store.path.read_bytes())
        self.assertFalse(any(sql.lstrip().upper().startswith(('CREATE ', 'INSERT ', 'DELETE ', 'ALTER ', 'BEGIN IMMEDIATE', 'PRAGMA JOURNAL_MODE')) for sql in statements), statements)

    def test_read_only_refuses_missing_old_and_drifted_schema_without_repair(self):
        missing = Path(self.tmp.name) / 'missing/wiki.db'
        with self.assertRaises(WikiError):
            WikiStore(missing, read_only=True)
        self.assertFalse(missing.parent.exists())
        with self.store.connection(write=True) as conn:
            conn.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
        with self.assertRaisesRegex(WikiError, '版本'):
            WikiStore(self.store.path, read_only=True)
        with self.store.connection(write=True) as conn:
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], '1')
            conn.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
            conn.execute('ALTER TABLE document_images RENAME COLUMN image_id TO broken_image_id')
        with self.assertRaisesRegex(WikiError, '结构不完整'):
            WikiStore(self.store.path, read_only=True)

    def test_read_only_connection_keeps_one_snapshot_across_concurrent_updates(self):
        reader = WikiStore(self.store.path, read_only=True)
        with reader.connection() as conn:
            old = conn.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0]
            self.add_source('concurrent')
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0], old)
        with reader.connection() as conn:
            self.assertNotEqual(conn.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0], old)

    def test_read_only_upstream_change_fails_closed_until_explicit_refresh(self):
        path = Path(self.tmp.name) / 'knowledge.db'
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute('CREATE TABLE raw_docs(kind,doc_id,game_version,locale,payload,source_url,sha256)')
            conn.execute("INSERT INTO raw_docs VALUES ('character','1210','3.7','zh',?,'https://example.invalid','old')", ('{"desc":"同步率上限为200。"}',))
        sync_nanoka(self.store, kind='character', doc_id='1210', knowledge_path=path, **SCOPE)
        self.publish(self.draft(sid='nanoka/character/1210'))
        reader = WikiStore(self.store.path, read_only=True)
        self.assertTrue(reader.search('同步率', **SCOPE))
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("UPDATE raw_docs SET sha256='new',payload=?", ('{"desc":"同步率上限为300。"}',))
        with self.assertRaisesRegex(WikiError, 'upstream_changed'):
            reader.search('同步率', **SCOPE)
        with reader.connection() as conn:
            self.assertEqual(conn.execute("SELECT current_revision FROM sources WHERE source_id='nanoka/character/1210'").fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT dirty FROM pages').fetchone()[0], 0)
        refreshed = self.store.refresh(**SCOPE)
        self.assertEqual(refreshed['refreshed'], 1)
        self.assertEqual(reader.search('同步率', **SCOPE), [])
        self.publish(self.draft(sid='nanoka/character/1210', rev=2, text='同步率上限为300。'))
        self.assertIn('300', reader.search('同步率', **SCOPE)[0]['snippet'])

    def test_read_only_upstream_missing_or_unavailable_is_not_silently_ignored(self):
        path = Path(self.tmp.name) / 'upstream.db'
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute('CREATE TABLE raw_docs(kind,doc_id,game_version,locale,payload,source_url,sha256)')
            conn.execute("INSERT INTO raw_docs VALUES ('character','1210','3.7','zh','{}','','old')")
        sync_nanoka(self.store, kind='character', doc_id='1210', knowledge_path=path, **SCOPE)
        reader = WikiStore(self.store.path, read_only=True)
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("UPDATE raw_docs SET doc_id='other'")
        with self.assertRaisesRegex(WikiError, 'upstream_missing_source'):
            reader.search('同步率', **SCOPE)
        # A different-version database retains the intentional historical policy.
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("UPDATE raw_docs SET game_version='3.8'")
        self.assertEqual(reader.search('同步率', **SCOPE), [])
        path.unlink()
        with self.assertRaisesRegex(WikiError, 'upstream_unavailable'):
            reader.search('同步率', **SCOPE)
        self.assertFalse(path.exists())

    def test_explicit_upstream_relocation_keeps_source_history_and_hash_checks(self):
        path = Path(self.tmp.name) / 'original.db'
        moved = Path(self.tmp.name) / 'container.db'
        for target in (path, moved):
            with closing(sqlite3.connect(target)) as conn, conn:
                conn.execute('CREATE TABLE raw_docs(kind,doc_id,game_version,locale,payload,source_url,sha256)')
                conn.execute("INSERT INTO raw_docs VALUES ('character','1210','3.7','zh',?,'https://example.invalid','old')", ('{"desc":"同步率上限为200。"}',))
        sync_nanoka(self.store, kind='character', doc_id='1210', knowledge_path=path, **SCOPE)
        self.publish(self.draft(sid='nanoka/character/1210'))
        path.unlink()
        reader = WikiStore(self.store.path, read_only=True)
        with patch.dict('os.environ', {'WIKI_UPSTREAM_DB_PATH': str(moved)}):
            self.assertTrue(reader.search('同步率', **SCOPE))
            self.assertEqual(reader.get_source('nanoka/character/1210', **SCOPE)['metadata']['upstream_db'], str(path))
            with closing(sqlite3.connect(moved)) as conn, conn:
                conn.execute("UPDATE raw_docs SET sha256='changed'")
            with self.assertRaisesRegex(WikiError, 'upstream_changed'):
                reader.search('同步率', **SCOPE)
        with patch.dict('os.environ', {'WIKI_UPSTREAM_DB_PATH': str(moved.with_name('missing.db'))}):
            with self.assertRaisesRegex(WikiError, 'upstream_unavailable'):
                reader.search('同步率', **SCOPE)

    def test_claims_without_resolved_citations_never_reach_snippet(self):
        self.publish()
        with patch.object(self.store, '_current_source', return_value=None):
            self.assertEqual(self.store.search('同步率', reviewed_only=True, **SCOPE), [])

    def test_claim_without_tool_citation_is_skipped_and_budget_drops_its_refs(self):
        second = '同步率持续20秒。'
        self.add_source('duration', raw=second)
        data = self.draft(text='同步率上限为200。' + second)
        data['body'] += ' [source:duration@1]'
        data['sources'].append({'source_id': 'duration', 'revision': 1})
        data['claims'] = [
            {'claim_id': cid, 'text': text, 'citations': [{'source_id': sid, 'revision': 1, 'quote': text}]}
            for cid, text, sid in [('cap', '同步率上限为200。', 'guide'), ('duration', second, 'duration')]
        ]
        self.publish(data)
        from llmwiki.payload import citations_for
        with patch('llmwiki.search.citations_for', side_effect=lambda page, claim: [] if claim['claim_id'] == 'cap' else citations_for(page, claim)):
            result = self.store.search('同步率', **SCOPE)[0]
        self.assertEqual([m['claim_id'] for m in result['matches']], ['duration'])
        self.assertNotIn('200', result['snippet'])
        full = self.store.search('同步率', **SCOPE)
        budget = len(json.dumps(full, ensure_ascii=False)) - 1
        bounded = self.store.search('同步率', max_chars=budget, **SCOPE)
        self.assertEqual(len(bounded[0]['matches']), 1)
        kept = {(ref['source_id'], ref['revision']) for m in bounded[0]['matches'] for ref in m['source_refs']}
        self.assertEqual({(ref['source_id'], ref['revision']) for ref in bounded[0]['source_refs']}, kept)
        self.assertLessEqual(len(json.dumps(bounded, ensure_ascii=False)), budget)

    def test_invalid_top_candidate_does_not_hide_valid_lower_candidate(self):
        self.publish(self.draft(pid='a-invalid'))
        self.add_source('valid')
        self.publish(self.draft(pid='z-valid', sid='valid'))
        current_source = self.store._current_source
        with patch.object(self.store, '_current_source', side_effect=lambda conn, sid, scope: None if sid == 'guide' else current_source(conn, sid, scope)):
            results = self.store.search('同步率', reviewed_only=True, limit=1, **SCOPE)
        self.assertEqual([r['page_id'] for r in results], ['z-valid'])
        self.assertTrue(all(m['source_refs'] for m in results[0]['matches']))

    def test_source_update_invalidates_transitive_pages_and_plan_order(self):
        self.publish(self.draft(pid="z-base"))
        summary = self.draft(pid="a-summary", pages=[{"page_id": "z-base", "revision": 1}])
        summary["body"] += " [page:z-base@1]"
        self.publish(summary)
        update = self.add_source(raw="同步率上限为300。")
        self.assertEqual(update["affected_pages"], ["a-summary", "z-base"])
        self.assertEqual(self.store.search("同步率", **SCOPE), [])
        self.assertEqual(self.store.plan(**SCOPE)["compile_order"], ["z-base", "a-summary"])
        with self.assertRaises(WikiError):
            self.store.get_page("z-base", **SCOPE)
        self.assertTrue(self.store.get_page("z-base", **SCOPE, allow_stale=True)["dirty"])

    def test_new_tagged_source_invalidates_matching_entity_only(self):
        self.add_source("one", entities=["character:1210"])
        self.add_source("two", entities=["character:1102"])
        self.publish(self.draft(pid="one", sid="one", entities=["character:1210"], topics=["mechanics"]))
        self.publish(self.draft(pid="two", sid="two", entities=["character:1102"], topics=["mechanics"]))
        result = self.add_source("extra", entities=["character:1210"])
        self.assertEqual(result["affected_pages"], ["one"])
        self.assertFalse(self.store.get_page("two", **SCOPE)["dirty"])
        prepared = self.store.prepare("one", **SCOPE)
        self.assertEqual({r["source_id"] for r in prepared["sources"]}, {"one", "extra"})
        invalid = self.store.stage_page(self.draft(pid="one", sid="one", entities=["character:1210"]))
        self.assertTrue(any("unreviewed_sources" in e for e in invalid["errors"]))

    def test_untagged_source_appears_in_uncompiled_queue(self):
        self.publish()
        result = self.add_source("unclassified")
        self.assertEqual(result["affected_pages"], [])
        self.assertEqual(self.store.plan(**SCOPE)["uncompiled_sources"], [{"source_id": "unclassified", "current_revision": 1}])

    def test_staged_source_becomes_stale_before_publish(self):
        stage = self.store.stage_page(self.draft())
        self.add_source(raw="同步率上限为300。")
        with self.assertRaisesRegex(WikiError, "stale_source"):
            publish_reviewed(self.store, stage["draft_id"])
        self.assertEqual(self.store.search("同步率", **SCOPE), [])

    def test_delete_restore_and_immutable_history(self):
        self.publish()
        deleted = self.store.delete_source("guide", **SCOPE)
        self.assertEqual(deleted["revision"], 2)
        self.assertFalse(self.store.delete_source("guide", **SCOPE)["changed"])
        self.assertTrue(self.store.get_source("guide", **SCOPE)["deleted"])
        self.assertEqual(self.store.get_source("guide", **SCOPE, revision=1)["text"], "同步率上限为200。")
        self.assertEqual(self.store.search("同步率", **SCOPE), [])
        self.assertEqual(self.add_source()["revision"], 3)
        self.publish(self.draft(rev=3))
        self.assertEqual(self.store.get_page("base", **SCOPE)["revision"], 2)
        self.assertEqual(self.store.get_page("base", **SCOPE, revision=1)["revision"], 1)
        with self.store.connection(write=True) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE source_revisions SET text='changed'")

    def test_wrong_quotes_conflicts_and_unregistered_body_citations_block_publish(self):
        bad = []
        quote = self.draft()
        quote["claims"][0]["citations"][0]["quote"] = "source never said this"
        bad.append(quote)
        bad.append(self.draft(unresolved_conflicts=["两个来源给出不同上限"]))
        body = self.draft()
        body["body"] += " [source:unknown@1]"
        bad.append(body)
        for payload in bad:
            stage = self.store.stage_page(payload)
            self.assertFalse(stage["valid"])
            with self.assertRaises(WikiError):
                publish_reviewed(self.store, stage["draft_id"])

    def test_page_race_and_prepare_base_revision(self):
        first = self.store.stage_page(self.draft())
        second = self.store.stage_page(self.draft(title="other"))
        publish_reviewed(self.store, first["draft_id"])
        with self.assertRaisesRegex(WikiError, "page_revision_changed"):
            publish_reviewed(self.store, second["draft_id"])
        self.assertEqual(self.store.prepare("base", **SCOPE)["draft_template"]["base_revision"], 1)

    def test_publication_index_failure_rolls_back_revision_and_outbox(self):
        self.publish()
        before = self.store.index_jobs()
        stage = self.store.stage_page(self.draft(title="new title"))
        with patch.object(self.store, "_index", side_effect=RuntimeError("index failure")):
            with self.assertRaises(RuntimeError):
                publish_reviewed(self.store, stage["draft_id"])
        self.assertEqual(self.store.get_page("base", **SCOPE)["revision"], 1)
        self.assertEqual(self.store.index_jobs(), before)
        self.assertEqual(len(self.store.search("资源", **SCOPE)), 1)

    def test_compilation_cycles_rejected(self):
        self.publish()
        self.publish(self.draft(pid="summary", pages=[{"page_id": "base", "revision": 1}]))
        stage = self.store.stage_page(self.draft(pages=[{"page_id": "summary", "revision": 1}]))
        self.assertTrue(any("dependency_cycle" in e for e in stage["errors"]))

    def test_scope_isolation_for_versions_and_languages(self):
        self.publish()
        self.assertEqual(self.store.search("同步率", game_version="3.8"), [])
        self.assertEqual(self.store.search("同步率", game_version="3.7", locale="en"), [])
        self.add_source(game_version="3.8", raw="同步率上限为400。")
        self.assertFalse(self.store.get_page("base", **SCOPE)["dirty"])

    def test_rebuild_index_and_reject_obsolete_vector_upserts(self):
        self.publish()
        job = self.store.index_jobs()[0]
        self.assertTrue(self.store.index_target(job["job_id"])["eligible_upsert"])
        self.add_source(raw="同步率上限为300。")
        self.assertFalse(self.store.index_target(job["job_id"])["eligible_upsert"])
        self.assertEqual(self.store.index_jobs()[-1]["operation"], "withdraw")
        self.assertEqual(self.store.rebuild_search()["indexed"], 0)
        self.publish(self.draft(rev=2, text="同步率上限为300。"))
        self.assertEqual(self.store.rebuild_search()["indexed"], 1)
        self.assertEqual(len(self.store.search("同步率", **SCOPE)), 1)

    def test_reject_other_database_before_creating_wiki_tables(self):
        path = Path(self.tmp.name) / "other.db"
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE important(value TEXT)")
        with self.assertRaises(WikiError):
            WikiStore(path)
        with closing(sqlite3.connect(path)) as conn, conn:
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='sources'").fetchone())

    def test_file_adapter_and_unsupported_formats(self):
        path = Path(self.tmp.name) / "guide.md"
        path.write_text("新机制资料。", encoding="utf-8")
        result = import_file(self.store, path, source_id="file", **SCOPE)
        self.assertEqual(result["revision"], 1)
        with self.assertRaises(WikiError):
            import_file(self.store, path.with_suffix(".pdf"), source_id="pdf", **SCOPE)

    def test_nanoka_adapter_reads_existing_mirror_without_mutation(self):
        path = Path(self.tmp.name) / "knowledge.db"
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE raw_docs(kind,doc_id,game_version,locale,payload,source_url,sha256)")
            conn.execute("INSERT INTO raw_docs VALUES ('character','1210','3.7','zh',?,?,'upstream')",
                         ('{"name":"爱弥斯"}', "https://example.invalid/1210"))
        before = path.read_bytes()
        sync_nanoka(self.store, kind="character", doc_id="1210", knowledge_path=path, **SCOPE)
        self.assertEqual(before, path.read_bytes())
        source = self.store.get_source("nanoka/character/1210", **SCOPE)
        self.assertEqual(source["metadata"]["entities"], ["character:1210"])
        self.assertEqual(source["metadata"]["upstream_sha256"], "upstream")

    def test_old_page_dependency_blocks_until_dependency_recompiled(self):
        self.publish()
        self.publish(self.draft(pid="summary", pages=[{"page_id": "base", "revision": 1}]))
        self.publish(self.draft(title="revised base"))
        self.assertTrue(self.store.get_page("summary", **SCOPE, allow_stale=True)["dirty"])
        stage = self.store.stage_page(self.draft(pid="summary", pages=[{"page_id": "base", "revision": 1}]))
        self.assertTrue(any("stale_page" in e for e in stage["errors"]))
        self.publish(self.draft(pid="summary", pages=[{"page_id": "base", "revision": 2}]))

    def test_prepare_reports_withdrawn_source_without_carrying_old_claims(self):
        self.publish()
        self.store.delete_source("guide", **SCOPE)
        prepared = self.store.prepare("base", **SCOPE)
        self.assertEqual(prepared["withdrawn_sources"], ["guide"])
        self.assertEqual(prepared["draft_template"]["claims"], [])
        self.assertEqual(prepared["draft_template"]["sources"], [])

    def test_fts_unavailable_uses_literal_search_without_serving_stale_pages(self):
        with self.store.connection(write=True) as conn:
            conn.execute("UPDATE meta SET value='0' WHERE key='fts'")
        self.publish()
        self.assertEqual(len(self.store.search("同步率", **SCOPE)), 1)
        self.add_source(raw="同步率上限为300。")
        self.assertEqual(self.store.search("同步率", **SCOPE), [])

    def test_invalid_json_and_invalid_draft_shapes_do_not_publish(self):
        with self.assertRaises(WikiError):
            self.store.import_source("bad", **SCOPE, title="bad", raw=b'{broken', media_type="application/json")
        for value in ({}, self.draft(sources=[{"source_id": "guide", "revision": True}])):
            with self.assertRaises(WikiError):
                self.store.stage_page(value)
        self.assertEqual(self.store.search("同步率", **SCOPE), [])

    def test_draft_version_cannot_reuse_other_versions_evidence(self):
        payload = self.draft(game_version="3.8")
        staged = self.store.stage_page(payload)
        self.assertFalse(staged["valid"])
        with self.assertRaisesRegex(WikiError, "stale_source"):
            publish_reviewed(self.store, staged["draft_id"])


if __name__ == "__main__":
    unittest.main()
