"""Isolated management auth, process dispatch, read-only plans and recovery tests."""
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import auth, config
from app.api.knowledge_ops import router
from app.services import knowledge_ops as ops
from llmwiki.store import WikiStore
from llmwiki.updates import atomic_json, UpdateBusy


class KnowledgeOpsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.wiki = root/'wiki/wiki.db'
        WikiStore(self.wiki)
        self.settings = replace(config.settings, knowledge_admin_user_ids=(1,))
        self.patches = [patch.object(ops,'ROOT',root/'wiki/operations'), patch.object(ops,'WIKI_PATH',self.wiki),
            patch.object(ops,'KNOWLEDGE_PATH',root/'knowledge.db'), patch.object(ops,'CACHE',root/'cache'),
            patch.object(ops,'settings',self.settings), patch.object(auth,'settings',self.settings)]
        for p in self.patches:p.start()
        self.addCleanup(lambda:[p.stop() for p in reversed(self.patches)])
        app = FastAPI(); app.include_router(router)
        self.app=app
        self.app.dependency_overrides[auth.current_user] = lambda:NS(user_id=1,is_guest=False)
        self.client=TestClient(app)
        self.addCleanup(self.client.close)

    def launch(self, recipe='inspect'):
        with patch.object(ops.subprocess,'Popen') as spawn:
            result=ops.start_job(recipe,1,launch=spawn)
        return result,spawn

    def test_non_admin_cannot_read_or_write_and_no_task_spawned(self):
        self.app.dependency_overrides[auth.current_user] = lambda:NS(user_id=2,is_guest=False)
        with patch.object(ops,'snapshot') as read,patch.object(ops,'start_job') as start:
            self.assertEqual(self.client.get('/api/v1/knowledge/ops').status_code,403)
            self.assertEqual(self.client.post('/api/v1/knowledge/ops/jobs',json={'recipe':'periods'}).status_code,403)
        read.assert_not_called();start.assert_not_called()

    def test_request_cannot_supply_paths_commands_or_unbounded_options(self):
        for body in [{'recipe':'shell'}, {'recipe':'periods','db':'C:/private.db'},
                     {'recipe':'periods','interval':0}, {'recipe':'roles','command':'bad'}]:
            self.assertEqual(self.client.post('/api/v1/knowledge/ops/jobs',json=body).status_code,422)

    def test_get_snapshot_and_plan_do_not_change_wiki(self):
        before=self.wiki.read_bytes()
        result=self.client.get('/api/v1/knowledge/ops')
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['counts']['pending_pages'],0)
        self.assertEqual(self.wiki.read_bytes(),before)
        job,_=self.launch()
        ops.execute_job(job['id'])
        self.assertEqual(ops.load_job(job['id'])['state'],'completed')
        self.assertEqual(self.wiki.read_bytes(),before)

    def test_only_one_job_and_no_shell_arguments(self):
        job,spawn=self.launch('periods')
        args,kw=spawn.call_args
        self.assertEqual(args[0], [sys.executable,'-m','app.services.knowledge_ops',job['id']])
        self.assertNotIn('shell',kw)
        with self.assertRaises(UpdateBusy):self.launch('official')

    def test_partial_native_report_resumes_same_run_and_options(self):
        job,_=self.launch('periods')
        report_id='a'*32
        def update(store,**kw):
            kw['progress']({'run_id':report_id,'phase':'build','page_status':'compiling'})
            report={'run_id':report_id,'state':'partial','phase':'finished','summary':{'published_pages':1},
                    'collection':{},'pages':[{'page_id':'chapter','status':'deferred'}]}
            atomic_json(self.wiki.parent/'updates'/report_id/'report.json',report)
            return report
        with patch.object(ops,'run_update',side_effect=update) as update_call:
            ops.execute_job(job['id'])
            with patch.object(ops.subprocess,'Popen') as spawn:
                resumed=ops.start_job('periods',1,resume=job['id'],launch=spawn)
            self.assertEqual(resumed['attempt'],2)
            ops.execute_job(job['id'])
        self.assertEqual(update_call.call_args.kwargs['resume'],report_id)
        self.assertEqual(update_call.call_args.kwargs['options'].interval,6)
        self.assertEqual(update_call.call_args.kwargs['options'].max_pages,12)

    def test_dead_process_is_interrupted_and_can_resume_without_writing_on_read(self):
        job,_=self.launch()
        raw=ops.load_job(job['id']);raw.update(state='running',pid=123456,heartbeat_at=ops.now())
        atomic_json(ops.job_path(job['id']),raw)
        before=ops.job_path(job['id']).read_bytes()
        with patch.object(ops,'alive',return_value=False):
            current=ops.public_job(ops.load_job(job['id']))
        self.assertEqual(current['state'],'interrupted');self.assertTrue(current['can_resume'])
        self.assertEqual(before,ops.job_path(job['id']).read_bytes())

    def test_dispatch_error_and_revoked_permission_fail_closed(self):
        with patch.object(ops.subprocess,'Popen',side_effect=OSError):
            job=ops.start_job('inspect',1,launch=ops.subprocess.Popen)
        self.assertEqual(job['state'],'failed')
        with patch.object(ops.subprocess,'Popen') as spawn:
            ops.start_job('inspect',1,resume=job['id'],launch=spawn)
        with patch.object(ops,'settings',replace(self.settings,knowledge_admin_user_ids=())),patch.object(ops,'snapshot') as read:
            ops.execute_job(job['id'])
        read.assert_not_called()
        self.assertEqual(ops.load_job(job['id'])['state'],'failed')

    def test_private_report_values_and_raw_errors_not_returned(self):
        data=ops.safe_report({'summary':{'published_pages':1,'token':'sk-private'},'cache_directory':'C:/private',
            'collection':{'official':{'status':'retryable_failure','errors':[{'message':'secret password','retryable':True,'retry_at':1793000000.0}]}},
            'fatal_error':{'message':'sk-private'}})
        rendered=json.dumps(data)
        for text in ('sk-private','secret password','C:/private'):self.assertNotIn(text,rendered)
        self.assertEqual(data['summary'],{'published_pages':1})
        self.assertTrue(data['issues'][0]['retry_at'].endswith('+00:00'))

    def test_invalid_job_ids_and_missing_data_do_not_create_files(self):
        for value in ('../secrets','', 'x'*32):
            with self.assertRaises(ValueError):ops.load_job(value)
        self.assertEqual(self.client.get('/api/v1/knowledge/ops/jobs/'+'a'*32).status_code,404)
        with patch.object(ops,'WIKI_PATH',Path(self.tmp.name)/'missing.db'):
            self.assertEqual(self.client.get('/api/v1/knowledge/ops').status_code,503)
        self.assertFalse((Path(self.tmp.name)/'missing.db').exists())

    def test_real_process_probe_is_read_only_and_no_signal_sent_on_windows(self):
        self.assertTrue(ops.alive(os.getpid()))
        self.assertFalse(ops.alive(-1))


if __name__=='__main__':unittest.main()
