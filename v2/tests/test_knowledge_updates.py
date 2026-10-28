"""Real local publication lifecycle, fake source network and bounded fake LLM."""
from collections import Counter
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from llmwiki import WikiStore, WikiError
from llmwiki.collector import Collector, CollectionError, DOCUMENT_SCOPE, import_document
from llmwiki.compiler import compile_page
from llmwiki.updates import UpdateOptions, UpdateLock, UpdateBusy, run_update, plan_documents, error_info
from wiki_test_helpers import publish_reviewed


class FakeClient:
    def __init__(self, directory, interval):
        self.log = []
        self.interval = interval


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = WikiStore(self.root / 'wiki.db')
        self.calls = Counter()
        self.version = 1
        self.options = UpdateOptions(sites=('mcguide',))

    def add_document(self, sid):
        doc = {'schema': 'dated-source-v1', 'kind': 'author_guide', 'title': '测试攻略', 'author': '测试作者',
               'source_date': None, 'date_basis': 'not_provided', 'game_version': None,
               'document_version': 'test-' + str(self.version),
               'content': {'baseTexts': [{'language': 'zh-Hans', 'introductionDetail':
                                         '聚爆模态推荐组合甲。' if self.version == 1 else '震谐模态推荐组合乙。'}]}}
        return import_document(self.store, sid, doc, origin='https://mcguide.kurogames.com/', entities=['character:1'])

    def collect(self, site, client, store, options, report, progress):
        self.calls['collect:' + site] += 1
        report['imported'].append(self.add_document('fixture/' + site))
        progress(report['imported'][-1])

    def compile(self, store, pid, **kwargs):
        self.calls['compile'] += 1
        def transport(messages, budget):
            self.calls['model'] += 1
            evidence = json.loads(messages[1]['content'])['evidence']
            result = {'sections': [{'heading': '正文', 'claims': [
                {'text': e['text'].replace('\n', ' '), 'evidence_ids': [e['evidence_id']]} for e in evidence]}],
                'open_questions': [], 'unresolved_conflicts': []}
            return {'content': json.dumps(result, ensure_ascii=False), 'finish_reason': 'stop', 'usage': {'completion_tokens': 100}}
        return compile_page(store, pid, **kwargs, transport=transport)

    def review(self, store, draft_id, **kwargs):
        self.calls['review'] += 1
        store.review(draft_id, decision='approved', reviewer='offline-fixture', reviewer_type='agent', note='Artificial expected text and citations checked.')
        return {'draft_id': draft_id, 'status': 'approved'}

    def run_batch(self, **kwargs):
        return run_update(self.store, directory=self.root / 'updates', cache_directory=self.root / 'cache',
                          options=kwargs.pop('options', self.options), client_factory=FakeClient,
                          collect_fn=kwargs.pop('collect_fn', self.collect), compile_fn=kwargs.pop('compile_fn', self.compile),
                          review_fn=kwargs.pop('review_fn', self.review), **kwargs)

    def latest_report(self):
        path = max((self.root / 'updates').glob('*/report.json'), key=lambda p:p.stat().st_mtime_ns)
        return json.loads(path.read_text(encoding='utf-8'))

    def test_new_unchanged_and_changed_sources_only_build_needed_pages(self):
        first = self.run_batch()
        self.assertEqual(first['state'], 'completed')
        self.assertEqual(first['summary']['new_sources'], 1)
        self.assertEqual(first['summary']['published_pages'], 1)
        second = self.run_batch()
        self.assertEqual(second['summary']['unchanged_pages'], 1)
        self.assertEqual(self.calls['model'], 1)
        self.version = 2
        third = self.run_batch()
        self.assertEqual(third['summary']['changed_sources'], 1)
        self.assertEqual(self.calls['model'], 2)
        page = self.store.get_page('document/fixture/mcguide/part-1', game_version=DOCUMENT_SCOPE)
        self.assertEqual(page['revision'], 2)
        self.assertIn('组合乙', page['page']['body'])
        self.assertTrue((self.root / 'updates' / first['run_id'] / 'before.db').exists())
        readable = (self.root / 'updates' / first['run_id'] / 'report.md').read_text(encoding='utf-8')
        self.assertIn('completed', readable)
        self.assertIn(first['run_id'], readable)

    def test_one_source_failure_does_not_erase_success_and_resume_skips_successful_collection(self):
        def collect(site, *args):
            if site == 'official' and not self.calls['official-error']:
                self.calls['official-error'] += 1
                raise CollectionError('timeout', retryable=True)
            self.collect(site, *args)
        first = self.run_batch(options=UpdateOptions(sites=('official', 'mcguide')), collect_fn=collect)
        self.assertEqual(first['state'], 'partial')
        self.assertEqual(first['summary']['published_pages'], 1)
        final = self.run_batch(resume=first['run_id'], collect_fn=collect)
        self.assertEqual(final['state'], 'completed')
        self.assertEqual(self.calls['collect:mcguide'], 1)
        self.assertEqual(self.calls['collect:official'], 1)
        self.assertEqual(self.calls['model'], 2)
        self.assertEqual(final['summary']['published_pages'], 2)

    def test_partial_collection_import_is_preserved_and_compiled(self):
        def interrupted_collection(site, *args):
            self.collect(site, *args)
            raise CollectionError('timeout after an imported source', retryable=True)
        report = self.run_batch(collect_fn=interrupted_collection)
        self.assertEqual(report['state'], 'partial')
        self.assertEqual(report['summary']['published_pages'], 1)
        self.assertEqual(len(report['collection']['mcguide']['imported']), 1)

    def test_interrupted_review_resumes_staged_draft_without_recompile(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_batch(review_fn=lambda *_a, **_kw: (_ for _ in ()).throw(KeyboardInterrupt()))
        report = self.latest_report()
        self.assertEqual(report['state'], 'interrupted')
        self.assertEqual(self.store.search('组合', game_version=DOCUMENT_SCOPE, reviewed_only=True), [])
        final = self.run_batch(resume=report['run_id'])
        self.assertEqual(final['state'], 'completed')
        self.assertEqual(self.calls['compile'], 1)
        self.assertEqual(self.calls['review'], 1)

    def test_crash_after_approval_recovers_publication_without_repeated_model_or_review(self):
        def approved_then_interrupted(store, draft_id, **kwargs):
            self.review(store, draft_id, **kwargs)
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_batch(review_fn=approved_then_interrupted)
        report = self.latest_report()
        final = self.run_batch(resume=report['run_id'])
        self.assertEqual(final['state'], 'completed')
        self.assertEqual(self.calls['compile'], 1)
        self.assertEqual(self.calls['review'], 1)

    def test_semantic_rejection_is_not_automatically_regenerated_on_resume_or_new_run(self):
        def reject(store, draft_id, **kwargs):
            self.calls['review'] += 1
            store.review(draft_id, decision='rejected', reviewer='fixture', reviewer_type='agent', note='Unsupported prerequisite')
            return {'status': 'needs_attention', 'draft_id': draft_id, 'error': '事实条件待处理'}
        first = self.run_batch(review_fn=reject)
        self.assertEqual(first['pages'][0]['status'], 'needs_attention')
        self.assertEqual(self.run_batch(resume=first['run_id'])['state'], 'partial')
        self.assertEqual(self.run_batch()['state'], 'partial')
        self.assertEqual(self.calls['model'], 1)
        self.version = 2
        self.assertEqual(self.run_batch()['state'], 'completed')
        self.assertEqual(self.calls['model'], 2)

    def test_transient_compile_failure_is_retryable_but_not_a_busy_loop(self):
        def fail(*args, **kwargs):
            self.calls['compile'] += 1
            return {'valid': False, 'errors': ['编译API失败: APITimeoutError'], 'attempts': []}
        first = self.run_batch(compile_fn=fail)
        self.assertEqual(self.calls['compile'], 1)
        self.assertEqual(first['pages'][0]['status'], 'retryable_failure')
        final = self.run_batch(resume=first['run_id'])
        self.assertEqual(final['state'], 'completed')
        self.assertEqual(self.calls['collect:mcguide'], 1)

    def test_failed_replacement_does_not_serve_previous_source_revision(self):
        self.run_batch()
        self.version = 2
        report = self.run_batch(compile_fn=lambda *_a, **_kw: {'valid': False, 'errors': ['unsupported facts']})
        self.assertEqual(report['state'], 'partial')
        self.assertEqual(self.store.search('组合甲', game_version=DOCUMENT_SCOPE, reviewed_only=True), [])

    def test_page_budget_defers_then_resume_builds_only_remaining_pages(self):
        first = self.run_batch(options=UpdateOptions(sites=('mcguide', 'official'), max_pages=1))
        self.assertEqual(first['summary']['deferred_pages'], 1)
        self.assertEqual(self.calls['model'], 1)
        final = self.run_batch(resume=first['run_id'])
        self.assertEqual(final['state'], 'completed')
        self.assertEqual(final['summary']['published_pages'], 2)
        self.assertEqual(self.calls['model'], 2)

    def test_obsolete_generated_chapters_are_withdrawn_not_deleted(self):
        self.run_batch()
        original = self.store.get_page('document/fixture/mcguide/part-1', game_version=DOCUMENT_SCOPE)['page']
        orphan = deepcopy(original)
        orphan.update(page_id='document/fixture/mcguide/part-2', base_revision=0)
        staged = self.store.stage_page(orphan)
        self.assertTrue(staged['valid'], staged)
        publish_reviewed(self.store, staged['draft_id'])
        report = self.run_batch()
        self.assertEqual(report['obsolete_pages'], [orphan['page_id']])
        self.assertTrue(self.store.get_page(orphan['page_id'], game_version=DOCUMENT_SCOPE, allow_stale=True)['dirty'])
        self.assertEqual(self.calls['model'], 1)

    def test_os_lock_blocks_concurrent_job_and_old_file_does_not_block_next_job(self):
        with UpdateLock(self.store.path):
            with self.assertRaises(UpdateBusy):
                self.run_batch()
        self.assertEqual(self.calls['model'], 0)
        self.assertEqual(self.run_batch()['state'], 'completed')

    def test_plan_has_no_network_or_model_calls(self):
        self.add_document('fixture/mcguide')
        plan = plan_documents(self.store)
        self.assertEqual(plan['pages'][0]['status'], 'pending')
        self.assertEqual(self.calls, Counter())
        self.assertFalse((self.root / 'updates').exists())

    def test_resume_rejects_other_database_and_traversal(self):
        first = self.run_batch()
        other = WikiStore(self.root / 'other.db')
        with self.assertRaises(WikiError):
            run_update(other, directory=self.root / 'updates', cache_directory=self.root / 'cache', resume=first['run_id'])
        with self.assertRaises(WikiError):
            self.run_batch(resume='../report')

    def test_actual_cooldown_survives_resume_without_another_http_request(self):
        starts = []
        def factory(directory, interval):
            c = Collector(directory, interval, clock=lambda:100000, sleep=lambda _:None)
            c.robots['guide-server.aki-game.com'] = ''
            return c
        def collect(site, client, *_args):
            client.fetch('https://guide-server.aki-game.com/test')
        def network(args, **kwargs):
            starts.append(1)
            Path(args[args.index('-D')+1]).write_text('HTTP/1.1 429\r\nRetry-After: 120\r\n', encoding='latin-1')
            Path(args[args.index('-o')+1]).write_bytes(b'limited')
            return SimpleNamespace(stdout=b'429', returncode=0)
        with patch('llmwiki.collector.subprocess.run', side_effect=network):
            first = run_update(self.store, directory=self.root/'updates', cache_directory=self.root/'cache', options=self.options,
                               client_factory=factory, collect_fn=collect)
            second = run_update(self.store, directory=self.root/'updates', cache_directory=self.root/'cache', resume=first['run_id'],
                                client_factory=factory, collect_fn=collect)
        self.assertEqual(len(starts), 1)
        self.assertEqual(second['state'], 'partial')
        self.assertEqual(second['collection']['mcguide']['errors'][0]['retry_at'], 100120)

    def test_invalid_limits_and_structured_error_classification(self):
        for kwargs in ({'interval': 1}, {'interval': float('inf')}, {'limit': 0}, {'max_pages': 101}, {'sites':('unknown',)}, {'articles':(1,2), 'limit':1}):
            with self.assertRaises(WikiError):
                UpdateOptions(**kwargs)
        self.assertTrue(error_info(WikiError('编译API失败: APIStatusError HTTP 503'))['retryable'])
        self.assertFalse(error_info(WikiError('编译API失败: AuthenticationError HTTP 401'))['retryable'])

    def test_internal_collection_error_can_be_resumed_after_a_fix(self):
        first = self.run_batch(collect_fn=lambda *_: (_ for _ in ()).throw(NameError('missing adapter import')))
        self.assertEqual(first['collection']['mcguide']['status'], 'retryable_failure')
        self.assertEqual(first['collection']['mcguide']['errors'][0]['error_type'], 'NameError')
        self.assertEqual(self.run_batch(resume=first['run_id'])['state'], 'completed')

    def test_review_transport_retry_reuses_the_existing_draft(self):
        first = self.run_batch(review_fn=lambda _s, draft_id, **_kw: {'draft_id':draft_id,'status':'needs_attention',
                                                                   'error':'编译API失败: APITimeoutError'})
        self.assertEqual(first['pages'][0]['status'], 'retryable_failure')
        self.assertEqual(self.run_batch(resume=first['run_id'])['state'], 'completed')
        self.assertEqual(self.calls['compile'], 1)

    def test_manual_approved_resolution_is_published_on_resume_without_new_review(self):
        def reject(store, draft_id, **kwargs):
            store.review(draft_id,decision='rejected',reviewer='fixture',reviewer_type='agent',note='Check source')
            return {'draft_id':draft_id,'status':'needs_attention','error':'事实待处理'}
        first = self.run_batch(review_fn=reject)
        with self.store.connection() as c:
            page = json.loads(c.execute('SELECT payload FROM drafts WHERE draft_id=?',(first['pages'][0]['draft_id'],)).fetchone()[0])
        page['semantic_review_parent'] = first['pages'][0]['draft_id']
        fixed = self.store.stage_page(page)
        self.store.review(fixed['draft_id'],decision='approved',reviewer='fixture-resolution',reviewer_type='agent',note='Explicit source-checked resolution')
        final = self.run_batch(resume=first['run_id'])
        self.assertEqual(final['state'], 'completed')
        self.assertEqual(self.calls['compile'], 1)
        self.assertEqual(self.calls['review'], 0)

    def test_approval_return_value_does_not_bypass_the_stored_review_gate(self):
        report = self.run_batch(review_fn=lambda _s, draft_id, **_kw: {'status':'approved','draft_id':draft_id})
        self.assertEqual(report['state'], 'partial')
        self.assertEqual(report['pages'][0]['status'], 'needs_attention')
        self.assertEqual(self.store.search('组合',game_version=DOCUMENT_SCOPE,reviewed_only=True), [])

    def test_source_change_during_review_cannot_publish_the_old_revision(self):
        def change_source(store, draft_id, **kwargs):
            self.version = 2
            self.add_document('fixture/mcguide')
            return self.review(store, draft_id, **kwargs)
        first = self.run_batch(review_fn=change_source)
        self.assertEqual(first['state'], 'partial')
        self.assertEqual(self.store.search('组合甲', game_version=DOCUMENT_SCOPE, reviewed_only=True), [])
        final = self.run_batch(resume=first['run_id'])
        self.assertEqual(final['state'], 'completed')
        self.assertEqual(self.calls['compile'], 2)


if __name__ == '__main__':
    unittest.main()
