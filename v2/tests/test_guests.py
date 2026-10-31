"""Guest isolation, shared quota races, expiry and migration; no real model calls."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine,event,select,func,inspect
from sqlalchemy.orm import sessionmaker
from app import auth,models
from app.api import auth as auth_api,chat as chat_api
from app.database import get_db
from app.main import app
from app.migrations import migrate,HEAD
from app.seed import seed_characters
from app.services import auth_service,guest_service,usage_service as usage


class GuestTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.engine=create_engine('sqlite:///'+(Path(self.tmp.name)/'app.db').as_posix(),
            connect_args={'check_same_thread':False,'timeout':15})
        event.listen(self.engine,'connect',lambda c,_:c.execute('PRAGMA foreign_keys=ON'))
        migrate(self.engine);self.factory=sessionmaker(self.engine)
        with self.factory() as db:
            seed_characters(db)
            db.add(models.User(username='registered',display_name='Registered',password_hash=auth_service.passwords.hash('Registered-pass-123')))
            db.commit()
        def dependency():
            with self.factory() as db:yield db
        app.dependency_overrides[get_db]=dependency
        config=replace(guest_service.settings,guest_enabled=True,guest_ip_secret='guest-fixture-secret-at-least-32-characters')
        self.patches=[patch.object(auth,'SessionLocal',self.factory),patch.object(auth_api,'SessionLocal',self.factory),
            patch.object(chat_api,'SessionLocal',self.factory),patch.object(guest_service,'settings',config),
            patch.object(auth_service,'settings',config),patch.object(usage,'settings',config)]
        for p in self.patches:p.start()
        self.a=TestClient(app,client=('203.0.113.10',1111));self.b=TestClient(app,client=('203.0.113.11',1111))

    def tearDown(self):
        self.a.close();self.b.close();app.dependency_overrides.clear()
        for p in reversed(self.patches):p.stop()
        self.engine.dispose();self.tmp.cleanup()

    def guest(self,client):
        r=client.post('/api/v1/auth/guest',json={})
        self.assertEqual(r.status_code,200,r.text)
        self.assertTrue(r.json()['user']['is_guest']);self.assertFalse(r.json()['user']['can_manage_knowledge'])
        client.headers['X-CSRF-Token']=r.json()['csrf_token']
        return r

    def admit(self,uid,sid=None):
        row,fresh=usage.admit(self.factory,uid,sid or uuid.uuid4().hex,uuid.uuid4().hex,'fixture question')
        self.assertTrue(fresh)
        return usage.Scope(row['id'],self.factory)

    def charge(self,scope):
        with usage.bound(scope):
            call=usage.reserve_call({'model':'deepseek-flash','messages':[],'max_tokens':16},'answer')
            usage.settle_call(call,{'prompt_tokens':10,'completion_tokens':1})
        usage.finish(scope,'completed')

    def test_entry_cookie_refresh_reuse_and_separate_visitors(self):
        first=self.guest(self.a);uid=first.json()['user']['id']
        self.assertIn('httponly',first.headers['set-cookie'].lower())
        self.assertIn('max-age=86400',first.headers['set-cookie'].lower())
        raw=self.a.cookies.get(auth.settings.auth_cookie_name)
        again=self.guest(self.a)
        self.assertEqual(again.json()['user']['id'],uid)
        self.assertEqual(self.a.cookies.get(auth.settings.auth_cookie_name),raw)
        self.assertEqual(self.a.get('/api/v1/auth/me').json()['user']['id'],uid)
        self.assertNotEqual(self.guest(self.b).json()['user']['id'],uid)
        with self.factory() as db:
            row=db.get(models.User,uid)
            self.assertNotEqual(row.guest_ip_hash,'203.0.113.10')
            self.assertIsNone(row.email)
            token=db.scalar(select(models.AuthSession).where(models.AuthSession.user_id==uid))
            self.assertLessEqual(token.expires_at,row.guest_expires_at)

    def test_guest_cannot_read_delete_stream_or_replay_another_identity(self):
        first=self.guest(self.a);self.guest(self.b)
        sid=self.a.post('/api/v1/chat/sessions',json={}).json()['id']
        self.assertEqual(self.b.get('/api/v1/chat/sessions').json(),[])
        self.assertEqual(self.b.get(f'/api/v1/chat/sessions/{sid}/messages').status_code,404)
        self.assertEqual(self.b.delete(f'/api/v1/chat/sessions/{sid}').status_code,404)
        self.assertEqual(self.b.post(f'/api/v1/chat/sessions/{sid}/messages',json={'content':'q'}).status_code,404)
        self.assertEqual(self.a.post('/api/v1/chat/sessions',json={'owner_id':first.json()['user']['id']+1}).status_code,422)

    def test_shared_daily_ip_quota_survives_cookie_clear_and_reentry(self):
        uid=self.guest(self.a).json()['user']['id']
        start=usage.now()
        for n in range(3):
            with patch.object(usage,'now',return_value=start+timedelta(seconds=n*61)):
                self.charge(self.admit(uid))
        with self.factory() as db:
            self.assertEqual(usage.overview(db,uid)['remaining_questions'],0)
            self.assertEqual(usage.overview(db,uid)['limits']['user_daily_questions'],3)
            names=usage.keys(uid,start)
            self.assertEqual(db.get(models.QuotaBucket,names[1]).questions,3)
            self.assertGreater(db.get(models.QuotaBucket,names[1]).spent_micros,0)
        self.a.cookies.clear()
        denied=self.a.post('/api/v1/auth/guest',json={})
        self.assertEqual(denied.status_code,429)
        self.assertIn('注册或登录',denied.text)
        # Another network still has an independent allowance.
        self.guest(self.b)

    def test_multiple_identities_on_same_ip_share_budget_and_concurrency(self):
        a=self.guest(self.a).json()['user']['id']
        self.a.cookies.clear();b=self.guest(self.a).json()['user']['id']
        scope=self.admit(a)
        with self.assertRaises(usage.QuotaError):self.admit(b)
        self.charge(scope)
        with self.factory() as db:self.assertEqual(usage.overview(db,b)['remaining_questions'],2)

    def test_last_shared_ip_question_is_atomic_under_concurrency(self):
        a=self.guest(self.a).json()['user']['id']
        self.a.cookies.clear();b=self.guest(self.a).json()['user']['id']
        with self.factory() as db:
            identity=db.get(models.User,a).guest_ip_hash
            usage.bucket(db,guest_service.ip_bucket(identity,usage.now())).questions=2;db.commit()
        def attempt(uid):
            try:self.admit(uid);return True
            except usage.QuotaError:return False
        with ThreadPoolExecutor(max_workers=2) as pool:self.assertEqual(list(pool.map(attempt,(a,b))).count(True),1)

    def test_failures_before_model_call_refund_guest_and_global_question_buckets(self):
        uid=self.guest(self.a).json()['user']['id'];scope=self.admit(uid)
        usage.finish(scope,'failed')
        with self.factory() as db:
            info=usage.overview(db,uid)
            self.assertEqual(info['remaining_questions'],3)
            for key in [*usage.keys(uid,usage.now())[:2],guest_service.ip_bucket(db.get(models.User,uid).guest_ip_hash,usage.now())]:
                self.assertEqual(db.get(models.QuotaBucket,key).questions,0)

    def test_guests_cannot_be_admin_even_if_the_id_was_accidentally_allowlisted(self):
        uid=self.guest(self.a).json()['user']['id']
        with patch.object(auth,'settings',replace(auth.settings,knowledge_admin_user_ids=(uid,))):
            self.assertEqual(self.a.get('/api/v1/usage/admin').status_code,403)
            self.assertEqual(self.a.get('/api/v1/knowledge/ops').status_code,403)

    def test_guest_cannot_log_in_with_generated_username(self):
        response=self.guest(self.a)
        self.assertEqual(self.b.post('/api/v1/auth/login',json={'username':response.json()['user']['username'],'password':'!guest-no-password'}).status_code,401)

    def test_expiry_denies_old_tokens_and_cleanup_keeps_billing_records(self):
        uid=self.guest(self.a).json()['user']['id']
        sid=self.a.post('/api/v1/chat/sessions',json={}).json()['id']
        self.charge(self.admit(uid,sid))
        with self.factory() as db:
            db.add(models.Message(session_id=sid,role='user',content='ephemeral'))
            db.get(models.User,uid).guest_expires_at=models.utcnow()-timedelta(seconds=1);db.commit()
        self.assertEqual(self.a.get('/api/v1/auth/me').status_code,401)
        self.assertEqual(self.a.get(f'/api/v1/chat/sessions/{sid}/messages').status_code,401)
        with self.factory() as db,db.begin():
            self.assertEqual(guest_service.cleanup(db),1)
        with self.factory() as db:
            self.assertIsNone(db.get(models.ChatSession,sid))
            self.assertEqual(db.scalar(select(func.count()).select_from(models.Message)),0)
            self.assertFalse(db.get(models.User,uid).is_active)
            self.assertIsNone(db.get(models.User,uid).guest_ip_hash)
            self.assertEqual(db.scalar(select(func.count()).select_from(models.GenerationRequest)),1)
            self.assertEqual(db.scalar(select(func.count()).select_from(models.LLMUsage)),1)
            self.assertFalse(db.get(models.User,1).is_guest)

    def test_rate_and_global_start_limit_and_foreign_origin(self):
        self.assertEqual(self.a.post('/api/v1/auth/guest',headers={'Origin':'https://evil.invalid'}).status_code,403)
        with patch.object(guest_service,'settings',replace(guest_service.settings,guest_daily_starts=1)):
            self.guest(self.a)
            self.assertEqual(self.b.post('/api/v1/auth/guest').status_code,429)

    def test_disabling_guests_and_missing_secret_fail_closed(self):
        with patch.object(guest_service,'settings',replace(guest_service.settings,guest_enabled=False)):
            self.assertEqual(self.a.post('/api/v1/auth/guest').status_code,503)
        with patch.object(guest_service,'settings',replace(guest_service.settings,guest_ip_secret='')):
            self.assertEqual(self.a.post('/api/v1/auth/guest').status_code,503)

    def test_regular_login_is_preserved_and_its_quota_unchanged(self):
        logged=self.a.post('/api/v1/auth/login',json={'username':'registered','password':'Registered-pass-123'})
        self.assertEqual(logged.status_code,200)
        self.assertFalse(logged.json()['user']['is_guest'])
        entered=self.a.post('/api/v1/auth/guest')
        self.assertEqual(entered.json()['user']['id'],logged.json()['user']['id'])
        quota=self.a.get('/api/v1/chat/quota').json()
        self.assertEqual(quota['remaining_questions'],20)
        self.assertEqual(quota['limits']['user_daily_questions'],20)

    def test_ipv6_subnet_and_mapped_ipv4_normalize_without_storing_addresses(self):
        self.assertEqual(guest_service.ip_hash('2001:db8::1'),guest_service.ip_hash('2001:db8::abcd'))
        self.assertEqual(guest_service.ip_hash('::ffff:203.0.113.10'),guest_service.ip_hash('203.0.113.10'))

    def test_previous_schema_upgrade_preserves_registered_accounts(self):
        migrate(self.engine,'0007_password_reset')
        self.assertNotIn('is_guest',{c['name'] for c in inspect(self.engine).get_columns('users')})
        self.assertEqual(migrate(self.engine)['revision'],HEAD)
        self.assertEqual(self.a.post('/api/v1/auth/login',json={'username':'registered','password':'Registered-pass-123'}).status_code,200)
