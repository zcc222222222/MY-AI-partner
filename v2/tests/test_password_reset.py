"""Recovery integration tests use temporary databases and captured SMTP only."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect, select
from sqlalchemy.orm import sessionmaker
from app import auth, models
from app.api import auth as auth_api
from app.main import app
from app.migrations import HEAD, migrate
from app.services import auth_service, email_verification, mail_service, password_reset


class PasswordResetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine('sqlite:///' + (Path(self.tmp.name)/'app.db').as_posix(),
                                    connect_args={'check_same_thread': False, 'timeout': 15})
        event.listen(self.engine, 'connect', lambda c, _: c.execute('PRAGMA foreign_keys=ON'))
        migrate(self.engine)
        self.sessions = sessionmaker(self.engine)
        with self.sessions() as db:
            db.add(models.User(username='alice', email='alice@example.com', email_verified_at=models.utcnow(),
                display_name='Alice', password_hash=auth_service.passwords.hash('Old-password-123')))
            db.commit()
        self.deliveries = []
        self.patches = [patch.object(auth, 'SessionLocal', self.sessions),
            patch.object(auth_api, 'SessionLocal', self.sessions),
            patch.object(password_reset, 'SessionLocal', self.sessions),
            patch.object(mail_service, 'require_mail'),
            patch.object(mail_service, 'send_password_reset_code', side_effect=lambda email, code:self.deliveries.append((email, code))),
            patch.object(password_reset, 'settings', replace(password_reset.settings, email_code_secret='test-secret-password-reset-at-least-32'))]
        for p in self.patches:p.start()
        self.a, self.b = TestClient(app), TestClient(app)

    def tearDown(self):
        self.a.close(); self.b.close()
        for p in reversed(self.patches):p.stop()
        self.engine.dispose(); self.tmp.cleanup()

    def code(self):
        response=self.a.post('/api/v1/auth/password-reset/code',json={'email':' ALICE@EXAMPLE.COM '})
        self.assertEqual(response.status_code,200,response.text)
        self.assertNotIn(self.deliveries[-1][1],response.text)
        return self.deliveries[-1][1]

    def body(self, code, **extra):
        return {'email':'alice@example.com','email_code':code,'new_password':'New-password-456',
                'confirm_password':'New-password-456',**extra}

    def test_reset_updates_hash_timestamp_revokes_all_sessions_and_keeps_history(self):
        for client in (self.a,self.b):
            self.assertEqual(client.post('/api/v1/auth/login',json={'username':'alice','password':'Old-password-123'}).status_code,200)
        with self.sessions() as db:
            db.add(models.Character(id=1,slug='fixture',name='Fixture',display_name='Fixture',system_prompt='test'))
            db.flush()
            db.add(models.ChatSession(id='kept',character_id=1,owner_id=1,title='kept'))
            db.flush()
            db.add(models.Message(session_id='kept',role='user',content='history stays'))
            db.commit()
        response=self.a.post('/api/v1/auth/password-reset',json=self.body(self.code()))
        self.assertEqual(response.status_code,200,response.text)
        self.assertNotIn('csrf_token',response.json())
        for client in (self.a,self.b):self.assertEqual(client.get('/api/v1/auth/me').status_code,401)
        with self.sessions() as db:
            user=db.get(models.User,1)
            self.assertTrue(auth_service.passwords.verify('New-password-456',user.password_hash))
            self.assertIsNotNone(user.password_changed_at)
            self.assertEqual(db.scalars(select(models.AuthSession)).all(),[])
            self.assertIsNone(db.get(models.PasswordResetChallenge,'alice@example.com'))
            self.assertEqual(db.scalar(select(models.Message.content)),'history stays')
        self.assertEqual(self.b.post('/api/v1/auth/login',json={'username':'alice','password':'Old-password-123'}).status_code,401)
        self.assertEqual(self.b.post('/api/v1/auth/login',json={'username':'alice','password':'New-password-456'}).status_code,200)

    def test_unknown_and_unverified_or_disabled_accounts_receive_identical_ack(self):
        known=self.a.post('/api/v1/auth/password-reset/code',json={'email':'alice@example.com'})
        unknown=self.a.post('/api/v1/auth/password-reset/code',json={'email':'unknown@example.com'})
        self.assertEqual(known.json(),unknown.json())
        with self.sessions() as db:
            user=db.get(models.User,1);user.email_verified_at=None
            db.get(models.PasswordResetChallenge,user.email).resend_after=models.utcnow()-timedelta(seconds=1)
            db.commit()
        unverified=self.a.post('/api/v1/auth/password-reset/code',json={'email':'alice@example.com'})
        self.assertEqual(unverified.json(),known.json())
        with self.sessions() as db:
            user=db.get(models.User,1);user.email_verified_at=models.utcnow();user.is_active=False
            db.get(models.PasswordResetChallenge,user.email).resend_after=models.utcnow()-timedelta(seconds=1)
            db.commit()
        disabled=self.a.post('/api/v1/auth/password-reset/code',json={'email':'alice@example.com'})
        self.assertEqual(disabled.json(),known.json())
        self.assertEqual(len(self.deliveries),1)

    def test_hmac_only_storage_and_wrong_guesses_are_persistent_and_bounded(self):
        code=self.code()
        with self.sessions() as db:
            row=db.get(models.PasswordResetChallenge,'alice@example.com')
            self.assertNotEqual(row.code_hash,code)
            self.assertEqual(len(row.code_hash),64)
            self.assertTrue(row.delivered)
        wrong='000000' if code!='000000' else '000001'
        for _ in range(5):self.assertEqual(self.a.post('/api/v1/auth/password-reset',json=self.body(wrong)).status_code,400)
        self.assertEqual(self.a.post('/api/v1/auth/password-reset',json=self.body(code)).status_code,400)
        with self.sessions() as db:self.assertEqual(db.get(models.PasswordResetChallenge,'alice@example.com').attempts,5)

    def test_resend_cooldown_replaces_old_code_and_success_cannot_replay(self):
        with patch.object(password_reset.secrets,'randbelow',return_value=111111):first=self.code()
        self.assertEqual(self.a.post('/api/v1/auth/password-reset/code',json={'email':'alice@example.com'}).status_code,429)
        with self.sessions() as db:
            db.get(models.PasswordResetChallenge,'alice@example.com').resend_after=models.utcnow()-timedelta(seconds=1);db.commit()
        with patch.object(password_reset.secrets,'randbelow',return_value=222222):second=self.code()
        self.assertEqual(self.a.post('/api/v1/auth/password-reset',json=self.body(first)).status_code,400)
        self.assertEqual(self.a.post('/api/v1/auth/password-reset',json=self.body(second)).status_code,200)
        self.assertEqual(self.a.post('/api/v1/auth/password-reset',json=self.body(second)).status_code,400)

    def test_expired_and_undelivered_codes_cannot_reset(self):
        code=self.code()
        with self.sessions() as db:
            row=db.get(models.PasswordResetChallenge,'alice@example.com');row.expires_at=models.utcnow()-timedelta(seconds=1);db.commit()
        self.assertEqual(self.a.post('/api/v1/auth/password-reset',json=self.body(code)).status_code,400)
        with self.sessions() as db:
            row=db.get(models.PasswordResetChallenge,'alice@example.com');row.expires_at=models.utcnow()+timedelta(minutes=10);row.delivered=False;db.commit()
        self.assertEqual(self.a.post('/api/v1/auth/password-reset',json=self.body(code)).status_code,400)

    def test_mail_failure_is_not_exposed_and_challenge_stays_unusable(self):
        with patch.object(mail_service,'send_password_reset_code',side_effect=HTTPException(503,'private SMTP error')):
            response=self.a.post('/api/v1/auth/password-reset/code',json={'email':'alice@example.com'})
        self.assertEqual(response.status_code,200)
        self.assertNotIn('private',response.text)
        with self.sessions() as db:self.assertFalse(db.get(models.PasswordResetChallenge,'alice@example.com').delivered)

    def test_password_policy_validation_redacts_submitted_secrets(self):
        for password in ('short12','abcdefghij','1234567890','password123',' spaced1234'):
            response=self.a.post('/api/v1/auth/password-reset',json=self.body('123456',new_password=password,confirm_password=password))
            self.assertEqual(response.status_code,422)
            self.assertNotIn(password,response.text)
        self.assertEqual(self.a.post('/api/v1/auth/password-reset',json=self.body('123456',confirm_password='Different-password-456')).status_code,422)

    def test_origins_and_shared_send_rate_limits_are_enforced(self):
        for path,payload in [('/code',{'email':'alice@example.com'}),('',self.body('123456'))]:
            self.assertEqual(self.a.post('/api/v1/auth/password-reset'+path,json=payload,headers={'Origin':'https://evil.invalid'}).status_code,403)
        for n in range(10):
            response=self.a.post('/api/v1/auth/password-reset/code',json={'email':f'unknown{n}@example.com'})
            self.assertEqual(response.status_code,200)
        self.assertEqual(self.a.post('/api/v1/auth/email-code',json={'email':'another@example.com'}).status_code,429)

    def test_registration_challenge_cannot_be_used_for_password_reset(self):
        with self.sessions() as db:
            db.add(models.EmailVerification(email='alice@example.com',nonce='a'*32,code_hash='b'*64,
                expires_at=models.utcnow()+timedelta(minutes=10),resend_after=models.utcnow(),attempts=0,delivered=True));db.commit()
        self.assertEqual(self.a.post('/api/v1/auth/password-reset',json=self.body('123456')).status_code,400)
        with patch.object(email_verification,'settings',password_reset.settings):
            self.assertNotEqual(password_reset.digest('alice@example.com','a'*32,'123456'),
                email_verification.digest('alice@example.com','a'*32,'123456'))

    def test_concurrent_consumers_only_one_can_change_password(self):
        code=self.code()
        def apply(n):
            with self.sessions() as db:
                try:password_reset.reset_password(db,'alice@example.com',code,f'Concurrent-password-{n}');return 200
                except HTTPException as exc:return exc.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(apply,(1,2)))
        self.assertEqual(sorted(results),[200,400])

    def test_login_verified_before_reset_cannot_issue_a_new_session_after_reset(self):
        code=self.code()
        with self.sessions() as stale:
            user=stale.get(models.User,1)
            original=user.password_hash
            self.assertEqual(self.a.post('/api/v1/auth/password-reset',json=self.body(code)).status_code,200)
            self.assertEqual(user.password_hash,original)
            with self.assertRaises(HTTPException) as caught:auth_service.issue_session(stale,user)
            self.assertEqual(caught.exception.status_code,401)

    def test_challenge_is_pinned_to_the_password_and_mailbox_at_issue(self):
        code=self.code()
        with self.sessions() as db:
            db.get(models.User,1).password_hash=auth_service.passwords.hash('Changed-before-reset-456');db.commit()
        self.assertEqual(self.a.post('/api/v1/auth/password-reset',json=self.body(code)).status_code,400)

    def test_existing_account_migrates_from_previous_release_without_password_changes(self):
        with self.sessions() as db:
            original=db.get(models.User,1).password_hash
        migrate(self.engine,'0006_usage_turns')
        self.assertNotIn('password_reset_challenges',inspect(self.engine).get_table_names())
        self.assertNotIn('password_changed_at',{c['name'] for c in inspect(self.engine).get_columns('users')})
        result=migrate(self.engine)
        self.assertEqual(result['revision'],HEAD)
        self.assertIsNotNone(result['backup'])
        with self.sessions() as db:
            user=db.get(models.User,1)
            self.assertEqual(user.password_hash,original)
            self.assertEqual(user.email,'alice@example.com')
            self.assertIsNone(user.password_changed_at)
        self.assertFalse(migrate(self.engine)['changed'])
