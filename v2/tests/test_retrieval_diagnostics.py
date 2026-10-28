"""Privacy, exact failure categories, retry bounds, permissions and isolation."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace as NS
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import auth,config
from app.api.knowledge_ops import router
from app.agents import tools,live_guides as live
from app.services import retrieval_diagnostics as diag,retrieval_recheck as retry
from llmwiki.updates import UpdateBusy


class RetrievalDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        patcher=patch.object(diag,'ROOT',Path(self.tmp.name)/'diagnostics');patcher.start();self.addCleanup(patcher.stop)
        self.app=FastAPI();self.app.include_router(router)
        self.app.dependency_overrides[auth.current_user]=lambda:NS(user_id=1)
        patcher=patch.object(auth,'settings',replace(config.settings,knowledge_admin_user_ids=(1,)))
        patcher.start();self.addCleanup(patcher.stop)
        self.client=TestClient(self.app);self.addCleanup(self.client.close)

    def test_read_does_not_create_directory_and_requires_admin(self):
        self.assertEqual(self.client.get('/api/v1/knowledge/ops/retrievals').json(),{'records':[]})
        self.assertFalse(diag.ROOT.exists())
        self.app.dependency_overrides[auth.current_user]=lambda:NS(user_id=2)
        self.assertEqual(self.client.get('/api/v1/knowledge/ops/retrievals').status_code,403)
        with patch.object(retry,'start') as start:
            self.assertEqual(self.client.post('/api/v1/knowledge/ops/retrievals/recheck',json={'topic':'逆境深塔'}).status_code,403)
        start.assert_not_called()

    def test_records_no_question_answer_username_raw_exception_or_arguments(self):
        secret='私人提问 sk-secret my-email@example.com'
        with patch.dict(tools._HANDLERS,{'find_gameplay_guides':lambda **kw:{'status':'links_found','results':[{'title':secret,'images':[{'url':secret}]}]}}):
            result=json.loads(tools.execute_tool('find_gameplay_guides',json.dumps({'query':'本期深塔 '+secret})))
        self.assertEqual(result['results'][0]['title'],secret)
        row=diag.recent()[0]
        self.assertEqual((row['topic'],row['reason'],row['result_count'],row['image_count']),('逆境深塔','success',1,1))
        self.assertNotIn('sk-secret',json.dumps(row))
        self.assertNotIn('example.com',next(diag.ROOT.glob('*.json')).read_text(encoding='utf-8'))
        self.assertNotIn('query',row)

    def test_failure_categories_and_concurrent_context_do_not_leak(self):
        def task(reason):
            def call():
                diag.observe(reason=reason,stage='search_response',cache='network')
                return {'status':'source_unavailable','retry_after_seconds':30}
            return diag.run('find_gameplay_guides',{'query':'深塔'},call)
        with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(task,['timeout','access_restricted']))
        self.assertEqual({r['reason'] for r in diag.recent()},{'timeout','access_restricted'})
        self.assertTrue(all(r['cache']=='network' for r in diag.recent()))
        diag.run('search_documents',{},lambda:{'status':'no_reviewed_match','results':[]})
        self.assertEqual(diag.recent()[0]['reason'],'no_match')

    def test_failure_and_write_errors_do_not_change_tool_contract(self):
        with patch.dict(tools._HANDLERS,{'search_documents':lambda **kw:(_ for _ in ()).throw(RuntimeError('private'))}):
            tools.execute_tool('search_documents','{}')
        self.assertEqual(diag.recent()[0]['reason'],'internal_error')
        self.assertNotIn('private',json.dumps(diag.recent()))
        with patch.object(diag,'atomic_json',side_effect=OSError),patch.dict(tools._HANDLERS,{'search_documents':lambda **kw:{'status':'found','results':[{}]}}):
            self.assertEqual(json.loads(tools.execute_tool('search_documents','{}'))['status'],'found')

    def test_running_record_survives_restart_and_is_read_only(self):
        record=diag.begin('find_gameplay_guides',{'query':'海墟'})
        before=diag.path_for(record['id']).read_bytes()
        with patch('app.services.knowledge_ops.alive',return_value=False):
            self.assertEqual(diag.recent()[0]['state'],'interrupted')
        self.assertEqual(diag.path_for(record['id']).read_bytes(),before)

    def test_interrupted_handler_does_not_leave_a_permanent_running_record(self):
        with self.assertRaises(KeyboardInterrupt):
            diag.run('find_gameplay_guides',{},lambda:(_ for _ in ()).throw(KeyboardInterrupt()))
        self.assertEqual(diag.recent()[0]['state'],'interrupted')
        self.assertIsNone(diag.OBSERVATION.get())

    def test_invalid_worker_json_is_classified_as_source_change(self):
        catalog=live.PublicCatalog(clock=lambda:0,sleep=lambda n:None)
        catalog.robots=NS(can_fetch=lambda *a:True);catalog.robots_until=1000
        def call():
            try: catalog.community('逆境深塔')
            except ValueError: return {'status':'source_unavailable'}
        with patch.object(catalog.browser,'search',side_effect=ValueError('Invalid worker JSON')):
            diag.run('find_gameplay_guides',{'query':'深塔'},call)
        self.assertEqual(diag.recent()[0]['reason'],'source_changed')

    def test_retry_cannot_supply_urls_questions_or_frequency(self):
        for body in ({'topic':'arbitrary'},{'topic':'逆境深塔','query':'private'},{'topic':'逆境深塔','interval':0}):
            self.assertEqual(self.client.post('/api/v1/knowledge/ops/retrievals/recheck',json=body).status_code,422)

    def test_retry_is_async_single_active_and_has_sixty_second_gap(self):
        with patch.object(live.CLIENT,'health',return_value={'status':'ok','wait_seconds':0}):
            job=retry.start('逆境深塔',dispatch=lambda r:None)
            self.assertEqual(job['state'],'running')
            with self.assertRaises(UpdateBusy):retry.start('终焉矩阵',dispatch=lambda r:None)
            record=diag.load(job['id']);record.update(state='completed');diag.atomic_json(diag.path_for(job['id']),record)
            with self.assertRaises(retry.CoolingDown):retry.start('终焉矩阵',dispatch=lambda r:None)

    def test_site_cooldown_cannot_be_bypassed_and_retry_after_is_returned(self):
        with patch.object(live.CLIENT,'health',return_value={'status':'access_restricted','wait_seconds':600}):
            response=self.client.post('/api/v1/knowledge/ops/retrievals/recheck',json={'topic':'逆境深塔'})
        self.assertEqual(response.status_code,429)
        self.assertEqual(response.headers['retry-after'],'600')
        self.assertFalse(diag.ROOT.exists() and list(diag.ROOT.glob('*.json')))

    def test_previous_admin_site_cooldown_survives_client_restart(self):
        with patch.object(live.CLIENT,'health',return_value={'status':'ok','wait_seconds':0}):
            job=retry.start('逆境深塔',dispatch=lambda r:None)
            record=diag.load(job['id'])
            record.update(state='completed',reason='access_restricted',finished_at=diag.now(),retry_after_seconds=600)
            diag.atomic_json(diag.path_for(job['id']),record)
            diag.atomic_json(diag.ROOT/'control/last-recheck.json',{'id':job['id'],
                'started_at':(datetime.now(timezone.utc)-timedelta(seconds=120)).isoformat()})
            with self.assertRaises(retry.CoolingDown) as result:retry.start('终焉矩阵',dispatch=lambda r:None)
        self.assertGreater(result.exception.seconds,590)

    def test_recheck_forces_fresh_call_without_private_arguments_or_model(self):
        record=diag.begin('find_gameplay_guides',{'query':'终焉矩阵'},origin='admin_recheck')
        def guide(query):
            self.assertTrue(diag.FORCE_REFRESH.get())
            self.assertEqual(query,'本期终焉矩阵攻略')
            diag.observe(cache='network',stage='search_response')
            return {'status':'period_unverified','results':[]}
        with patch.object(live,'find_gameplay_guides',side_effect=guide):retry.execute(record)
        self.assertFalse(diag.FORCE_REFRESH.get())
        self.assertEqual(diag.recent()[0]['reason'],'period_unverified')

    def test_force_refresh_skips_valid_cache_but_keeps_rate_limit(self):
        catalog=live.PublicCatalog(clock=lambda:0,sleep=lambda n:None)
        catalog.cache[('community','逆境深塔')]=(300,{'status':'ok','posts':[]})
        catalog.next_at=600;catalog.robots=NS(can_fetch=lambda *a:True);catalog.robots_until=1000
        token=diag.FORCE_REFRESH.set(True)
        try:
            with patch.object(catalog.browser,'search') as launch:
                with self.assertRaises(ValueError):catalog.community('逆境深塔')
            launch.assert_not_called()
        finally:diag.FORCE_REFRESH.reset(token)

    def test_retention_is_bounded_and_does_not_delete_unrelated_files(self):
        diag.ROOT.mkdir(parents=True)
        foreign=diag.ROOT/'keep-me.json';foreign.write_text('{"private":"unrelated"}',encoding='utf-8')
        for index in range(301):
            ident=f'{index:032x}'
            path=diag.path_for(ident)
            path.write_text(json.dumps({'schema':'retrieval-diagnostic-v1','id':ident,'state':'completed'}))
            os.utime(path,(1000+index,1000+index))
        protected=f'{0:032x}'
        diag.atomic_json(diag.ROOT/'control/last-recheck.json',{'id':protected})
        diag.begin('search_documents',{})
        self.assertTrue(foreign.exists())
        self.assertTrue(diag.path_for(protected).exists())
        self.assertEqual(len(list(diag.ROOT.glob('*.json'))),301)


if __name__=='__main__':unittest.main()
