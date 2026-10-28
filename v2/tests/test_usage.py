"""Isolated ledger races, request lifetime, all model entry points and HTTP ownership."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch,Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from sqlalchemy import create_engine,event,select,func
from sqlalchemy.orm import sessionmaker
from app import models,llm,auth
from app.migrations import migrate
from app.services import usage_service as usage
from app.agents import intent
from app.services import context_service
import test_auth as auth_fixtures


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.engine=create_engine('sqlite:///'+(Path(self.tmp.name)/'app.db').as_posix(),connect_args={'check_same_thread':False,'timeout':10})
        event.listen(self.engine,'connect',lambda conn,_:conn.execute('PRAGMA journal_mode=WAL'))
        self.addCleanup(self.engine.dispose);migrate(self.engine);self.factory=sessionmaker(self.engine,autoflush=False)
        with self.factory() as db:
            for i in range(1,5):db.add(models.User(id=i,username='test'+str(i),display_name='test',password_hash='fixture'))
            db.commit()
        self.moment=[datetime(2026,10,27,9)]
        patcher=patch.object(usage,'now',side_effect=lambda:self.moment[0]);patcher.start();self.addCleanup(patcher.stop)

    def start(self,owner=1,client=None,session=None):
        info,fresh=usage.admit(self.factory,owner,session or 'session'+str(owner),client or ('request-'+str(owner)).ljust(20,'0'),'question')
        self.assertTrue(fresh);return usage.Scope(info['id'],self.factory)

    def call(self,scope,output=128):
        with usage.bound(scope):return usage.reserve_call({'model':'deepseek-flash','messages':[{'role':'user','content':'q'}],'max_tokens':output},'answer')

    def count(self,table):
        with self.factory() as db:return db.scalar(select(func.count()).select_from(table))

    def test_defaults_match_approved_values(self):
        values=usage.limits()
        self.assertEqual([values[k] for k in ('user_daily_questions','global_daily_questions','user_daily_micros','global_daily_micros','global_monthly_micros','user_concurrency','global_concurrency','user_rpm')],
                         [20,200,500000,5000000,50000000,1,3,3])

    def test_duplicate_and_changed_payload_are_distinguished(self):
        scope=self.start()
        previous,fresh=usage.admit(self.factory,1,'session1','request-1'.ljust(20,'0'),'question')
        self.assertFalse(fresh);self.assertEqual(previous['id'],scope.request_id)
        with self.assertRaises(usage.QuotaError):usage.admit(self.factory,1,'session1','request-1'.ljust(20,'0'),'changed')
        self.assertEqual(self.count(models.GenerationRequest),1)

    def test_last_daily_question_is_atomic_between_users(self):
        def attempt(i):
            try:return usage.admit(self.factory,i,'s'+str(i),str(i)*20,'q')[1]
            except usage.QuotaError:return False
        with patch.object(usage,'settings',replace(usage.settings,quota_global_daily_questions=1)):
            with ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(attempt,[1,2]))
        self.assertEqual(result.count(True),1)
        self.assertEqual(self.count(models.GenerationRequest),1)

    def test_last_cost_budget_is_reserved_atomically(self):
        scopes=[self.start(1),self.start(2)]
        def attempt(scope):
            try:self.call(scope);return True
            except usage.QuotaError:return False
        with patch.object(usage,'settings',replace(usage.settings,quota_global_daily_micros=15000)):
            with ThreadPoolExecutor(max_workers=2) as pool:result=list(pool.map(attempt,scopes))
        self.assertEqual(result.count(True),1)

    def test_rpm_user_and_global_concurrency_and_session_lock(self):
        self.start()
        for owner,session in ((1,'other-session'),(2,'session1')):
            with self.assertRaises(usage.QuotaError):self.start(owner,client=str(owner)*20,session=session)
        self.start(2);self.start(3)
        with self.assertRaises(usage.QuotaError):self.start(4)

    def test_sliding_minute_includes_failed_and_refunded_requests(self):
        for i in range(3):
            scope=self.start(client=str(i)*20);usage.finish(scope,'failed')
        with self.assertRaises(usage.QuotaError):self.start(client='fourth-request-000000')
        self.moment[0]+=timedelta(seconds=61)
        self.start(client='after-window-0000000')

    def test_settlement_releases_unused_reservation_and_is_idempotent(self):
        scope=self.start();call=self.call(scope)
        with usage.bound(scope):
            usage.settle_call(call,{'prompt_tokens':100,'completion_tokens':10,'prompt_cache_hit_tokens':50})
            usage.settle_call(call,{'prompt_tokens':10000,'completion_tokens':1000})
        usage.finish(scope,'completed')
        with self.factory() as db:
            ledger=db.get(models.LLMUsage,call)
            self.assertEqual(ledger.charged_micros,182)
            self.assertEqual(ledger.status,'reported')
            for name in usage.keys(1,self.moment[0]):
                row=db.get(models.QuotaBucket,name)
                self.assertEqual(row.spent_micros,182);self.assertEqual(row.reserved_micros,0)

    def test_unknown_usage_and_cancel_are_charged_conservatively(self):
        scope=self.start();call=self.call(scope)
        usage.finish(scope,'cancelled');usage.finish(scope,'cancelled')
        with self.factory() as db:
            row=db.get(models.LLMUsage,call)
            self.assertEqual(row.status,'estimated_unknown');self.assertEqual(row.charged_micros,row.reserved_micros)
            self.assertEqual(db.get(models.GenerationRequest,scope.request_id).status,'cancelled')

    def test_expired_lease_recovers_slots_and_old_call_cannot_continue(self):
        first=self.start();self.call(first)
        self.moment[0]+=timedelta(seconds=301)
        second=self.start(client='second-request-00000')
        with self.factory() as db:self.assertEqual(db.get(models.GenerationRequest,first.request_id).status,'interrupted')
        with self.assertRaises(usage.QuotaError):self.call(first)
        self.assertNotEqual(first.request_id,second.request_id)

    def test_crash_after_persisted_answer_keeps_replay_identity(self):
        first=self.start();self.call(first);usage.record_answer(first,123)
        self.moment[0]+=timedelta(seconds=301)
        self.start(client='new-request-after-crash')
        with self.factory() as db:
            row=db.get(models.GenerationRequest,first.request_id)
            self.assertEqual(row.status,'completed');self.assertEqual(row.assistant_message_id,123)

    def test_no_model_call_refunds_question_but_keeps_rate_record(self):
        scope=self.start();usage.finish(scope,'failed')
        with self.factory() as db:
            self.assertEqual(usage.overview(db,1)['remaining_questions'],20)
        self.assertEqual(self.count(models.GenerationRequest),1)

    def test_disconnect_after_answer_persistence_keeps_completed_replay_identity(self):
        scope=self.start()
        usage.record_answer(scope,123)
        usage.finish(scope,'cancelled')
        with self.factory() as db:
            row=db.get(models.GenerationRequest,scope.request_id)
            self.assertEqual(row.status,'completed')
            self.assertEqual(row.assistant_message_id,123)

    def test_disconnect_before_generator_entry_refunds_admission(self):
        import anyio
        from app.api.generation_response import GenerationResponse
        scope=self.start()
        started=[]
        def source():
            started.append(True)
            yield 'unexpected'
        class NeverStarted(GenerationResponse):
            async def stream_response(self,send):
                await anyio.sleep_forever()
        async def exercise():
            async def receive():return {'type':'http.disconnect'}
            async def send(message):pass
            await NeverStarted(source(),scope)({'type':'http'},receive,send)
        anyio.run(exercise)
        self.assertEqual(started,[])
        with self.factory() as db:
            self.assertEqual(db.get(models.GenerationRequest,scope.request_id).status,'cancelled')
            self.assertEqual(usage.overview(db,1)['remaining_questions'],20)

    def test_cancelled_request_cannot_reserve_or_start_another_provider_call(self):
        scope=self.start()
        scope.cancel()
        with usage.bound(scope),patch.object(llm,'_get_client') as client:
            with self.assertRaises(usage.GenerationCancelled):
                llm.completion(purpose='intent',model='deepseek-flash',max_tokens=128,messages=[])
            client.assert_not_called()
        usage.finish(scope,'cancelled')
        self.assertEqual(self.count(models.LLMUsage),0)

    def test_beijing_daily_reset_and_month_budget(self):
        scope=self.start();call=self.call(scope)
        with usage.bound(scope):usage.settle_call(call,{'prompt_tokens':100,'completion_tokens':10})
        usage.finish(scope,'completed');self.moment[0]=datetime(2026,10,27,16)
        with self.factory() as db:
            value=usage.overview(db,1,admin=True)
            self.assertEqual(value['remaining_questions'],20)
            self.assertGreater(value['global_month']['spent_micros'],0)
        with patch.object(usage,'settings',replace(usage.settings,quota_global_monthly_micros=1)):
            with self.assertRaises(usage.QuotaError):self.start(client='next-day-request-000')

    def test_pause_persists_and_denies_next_call(self):
        scope=self.start()
        with self.factory() as db:usage.pause(db,True)
        with self.assertRaises(usage.QuotaError):self.call(scope)
        with self.factory() as db:usage.pause(db,False)
        self.call(scope)

    def test_title_intent_summary_and_stream_use_same_ledger(self):
        scope=self.start();client=Mock();client.with_options.return_value=client
        response=NS(choices=[NS(finish_reason='stop',message=NS(content='title'))],usage=NS(model_dump=lambda:{'prompt_tokens':100,'completion_tokens':10}))
        client.chat.completions.create.return_value=response
        with usage.bound(scope),patch.object(llm,'_get_client',return_value=client):
            llm.chat([{'role':'user','content':'q'}])
            response.choices[0].message.content='{"topics":["chat"]}'
            intent.extract({})
            response.choices[0].message.content='{"message_ids":[1]}'
            context_service.select_memory([{'message_id':1,'user_text':'q'}])
            stream=Mock();stream.__iter__=Mock(return_value=iter([NS(usage=response.usage,choices=[])]))
            client.chat.completions.create.return_value=stream
            list(llm.stream_completion([{'role':'user','content':'q'}],max_tokens=128))
        with self.factory() as db:
            rows=db.scalars(select(models.LLMUsage)).all()
            self.assertEqual({r.purpose for r in rows},{'title','intent','summary','answer'})
            self.assertTrue(all(r.status=='reported' for r in rows))
        self.assertTrue(any(call.kwargs.get('max_retries')==0 for call in client.with_options.call_args_list))

    def test_stream_close_charges_unknown_usage_and_closes_upstream(self):
        scope=self.start();closed=[]
        class Stream:
            def __iter__(self):
                yield NS(usage=None,choices=[NS(finish_reason=None,delta=NS(content='partial',tool_calls=None))])
            def close(self):closed.append(True)
        client=Mock();client.with_options.return_value=client;client.chat.completions.create.return_value=Stream()
        with usage.bound(scope),patch.object(llm,'_get_client',return_value=client):
            stream=llm.stream_completion([{'role':'user','content':'q'}],max_tokens=128)
            next(stream);stream.close()
        self.assertTrue(closed)
        with self.factory() as db:self.assertEqual(db.scalar(select(models.LLMUsage)).status,'estimated_unknown')

    def test_unsupported_model_and_missing_budget_fail_before_provider(self):
        scope=self.start()
        with usage.bound(scope),patch.object(llm,'_get_client') as client:
            with self.assertRaises(usage.QuotaError):llm.completion(purpose='title',model='unknown',max_tokens=128,messages=[])
            client.assert_not_called()

    def test_budget_exhaustion_stops_provider_and_has_no_reservation_leak(self):
        scope=self.start()
        with patch.object(usage,'settings',replace(usage.settings,quota_global_daily_micros=1)),usage.bound(scope),patch.object(llm,'_get_client') as client:
            with self.assertRaises(usage.QuotaError):llm.chat([{'role':'user','content':'q'}])
            client.assert_not_called()
        self.assertEqual(self.count(models.LLMUsage),0)

    def test_heartbeat_deadline_and_account_pause(self):
        scope=self.start()
        with self.factory() as db:usage.pause(db,True,1)
        with self.assertRaises(usage.QuotaError):self.call(scope)
        with self.factory() as db:usage.pause(db,False,1)
        self.moment[0]+=timedelta(seconds=20)
        with usage.bound(scope):usage.heartbeat(force=True)
        self.moment[0]+=timedelta(seconds=281)
        with usage.bound(scope),self.assertRaises(usage.QuotaError):usage.heartbeat(force=True)

    def test_restart_reads_persisted_usage_and_reservations(self):
        scope=self.start();self.call(scope)
        new_factory=sessionmaker(self.engine,autoflush=False)
        with new_factory() as db:
            value=usage.overview(db,1,admin=True)
            self.assertGreater(value['user']['reserved_micros'],0)
            self.assertEqual(value['remaining_questions'],19)
            self.assertEqual(value['users'][0]['owner_id'],1)

    def test_failed_turn_is_visible_in_audit_but_excluded_from_model_context(self):
        first=self.start()
        with usage.bound(first):usage.record_user(12)
        usage.finish(first,'failed')
        second=self.start(client='new-context-request-01')
        with self.factory() as db,usage.bound(second):
            rows=usage.context_history(db,[NS(id=11),NS(id=12),NS(id=13)],1,'session1')
            self.assertEqual([row.id for row in rows],[11,13])
            self.assertEqual(db.get(models.GenerationRequest,first.request_id).user_message_id,12)


class UsageHTTPTests(unittest.TestCase):
    setUp=auth_fixtures.AuthTests.setUp
    tearDown=auth_fixtures.AuthTests.tearDown
    register=auth_fixtures.AuthTests.register
    registration_body=auth_fixtures.AuthTests.registration_body

    def test_http_duplicate_replays_and_ownership_isolation(self):
        self.register(self.a,'alice');self.register(self.b,'bob')
        sid=self.a.post('/api/v1/chat/sessions',json={}).json()['id'];body={'content':'q','client_request_id':'http-request-0000001'}
        def generate(db,session,character,content,**kwargs):
            from app.services.session_service import append_message
            append_message(db,session.id,'user',content,owner_id=session.owner_id)
            row=append_message(db,session.id,'assistant','answer',owner_id=session.owner_id)
            yield json.dumps({'type':'delta','content':'answer'})
            yield json.dumps({'type':'done','message_id':row.id})
        with patch.object(auth_fixtures.chat_service,'stream_reply',side_effect=generate) as run:
            first=self.a.post(f'/api/v1/chat/sessions/{sid}/messages',json=body)
            second=self.a.post(f'/api/v1/chat/sessions/{sid}/messages',json=body)
            self.assertEqual(first.status_code,200,first.text);self.assertEqual(second.status_code,200,second.text)
            self.assertIn('answer',second.text);self.assertEqual(run.call_count,1)
        self.assertEqual(self.b.get('/api/v1/chat/requests/'+body['client_request_id']).status_code,404)
        self.assertNotIn('global_day',self.b.get('/api/v1/chat/quota').json())

    def test_admin_usage_requires_role_and_csrf(self):
        from app import config
        self.register(self.a,'alice');self.register(self.b,'bob')
        with patch.object(auth,'settings',replace(config.settings,knowledge_admin_user_ids=(1,))):
            self.assertEqual(self.a.get('/api/v1/usage/admin').status_code,200)
            self.assertEqual(self.b.get('/api/v1/usage/admin').status_code,403)
            csrf=self.a.headers.pop('X-CSRF-Token')
            self.assertEqual(self.a.post('/api/v1/usage/admin/pause',json={'paused':True}).status_code,403)
            self.a.headers['X-CSRF-Token']=csrf
            self.assertEqual(self.a.post('/api/v1/usage/admin/pause',json={'paused':True}).status_code,200)
            self.assertEqual(self.a.get('/api/v1/usage/admin').headers['cache-control'],'no-store')
            self.assertTrue(self.a.get('/api/v1/chat/quota').json()['paused'])


if __name__=='__main__':unittest.main()
