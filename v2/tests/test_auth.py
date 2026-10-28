"""Offline HTTP isolation and migration regressions; never uses the configured DB."""
from datetime import timedelta
from pathlib import Path
import sys
import tempfile
import os
import subprocess
import unittest
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.orm import sessionmaker
from app.main import app
from app import auth, models
from app.api import chat as chat_api
from app.api import auth as auth_api
from app.database import get_db
from app.migrations import migrate
from app.seed import seed_characters
from app.services import auth_service, chat_service, email_verification, mail_service


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + (Path(self.tmp.name) / "app.db").as_posix(), connect_args={"check_same_thread": False})
        event.listen(self.engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
        migrate(self.engine)
        self.sessions = sessionmaker(self.engine)
        with self.sessions() as db:
            seed_characters(db)
        def dependency():
            with self.sessions() as db:
                yield db
        app.dependency_overrides[get_db] = dependency
        self.deliveries = {}
        def capture_mail(email, code):
            self.deliveries[email] = code
        self.patches = [patch.object(auth, "SessionLocal", self.sessions), patch.object(chat_api, "SessionLocal", self.sessions), patch.object(auth_api, "SessionLocal", self.sessions),
            patch.object(mail_service, "require_mail"), patch.object(mail_service, "send_registration_code", side_effect=capture_mail),
            patch.object(email_verification, "settings", replace(email_verification.settings, email_code_secret="test-secret-key-for-email-at-least-32"))]
        for p in self.patches:
            p.start()
        # Do not enter TestClient's lifespan: it targets the real configured DB.
        self.a, self.b = TestClient(app), TestClient(app)

    def tearDown(self):
        self.a.close(); self.b.close()
        app.dependency_overrides.clear()
        for p in self.patches:
            p.stop()
        self.engine.dispose()
        self.tmp.cleanup()

    def register(self, client, name):
        body = self.registration_body(client, name)
        r = client.post("/api/v1/auth/register", json=body)
        self.assertEqual(r.status_code, 201, r.text)
        client.headers["X-CSRF-Token"] = r.json()["csrf_token"]
        return r

    def registration_body(self, client, name):
        email = name + "@example.com"
        r = client.post("/api/v1/auth/email-code", json={"email": email})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn(self.deliveries[email], r.text)
        return {"username": name, "password": "password-test-123", "confirm_password": "password-test-123",
                "email": email, "email_code": self.deliveries[email]}

    def test_two_accounts_cannot_list_read_delete_or_stream_each_others_chats(self):
        self.assertEqual(self.a.get("/api/v1/chat/sessions").status_code, 401)
        self.register(self.a, "alice")
        self.register(self.b, "bob")
        r = self.a.post("/api/v1/chat/sessions", json={})
        self.assertEqual(r.status_code, 200, r.text)
        sid = r.json()["id"]
        with self.sessions() as db:
            db.add(models.ChatSession(id="legacy", character_id=1, title="private legacy"))
            db.commit()
        self.assertEqual(len(self.a.get("/api/v1/chat/sessions").json()), 1)
        self.assertEqual(self.b.get("/api/v1/chat/sessions").json(), [])
        for client in [self.a, self.b]:
            self.assertEqual(client.get("/api/v1/chat/sessions/legacy/messages").status_code, 404)
        with patch.object(chat_service, "run_agent") as llm:
            for method, path, kwargs in [
                (self.b.get, f"/api/v1/chat/sessions/{sid}/messages", {}),
                (self.b.delete, f"/api/v1/chat/sessions/{sid}", {}),
                (self.b.post, f"/api/v1/chat/sessions/{sid}/messages", {"json": {"content": "secret?"}}),
            ]:
                self.assertEqual(method(path, **kwargs).status_code, 404)
            llm.assert_not_called()
        self.assertEqual(self.a.post("/api/v1/chat/sessions", json={"owner_id": 2}).status_code, 422)

    def test_password_cookie_csrf_origin_logout_and_expiry(self):
        response = self.register(self.a, "alice")
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=lax", cookie)
        raw = self.a.cookies.get(auth.settings.auth_cookie_name)
        with self.sessions() as db:
            user = db.scalar(select(models.User))
            self.assertTrue(user.password_hash.startswith("$argon2id$"))
            self.assertNotIn("password-test-123", user.password_hash)
            stored = db.scalar(select(models.AuthSession))
            self.assertNotEqual(raw, stored.token_hash)
        self.assertEqual(self.a.get("/api/v1/auth/me").headers["cache-control"], "no-store")
        csrf = self.a.headers.pop("X-CSRF-Token")
        self.assertEqual(self.a.post("/api/v1/chat/sessions", json={}).status_code, 403)
        self.a.headers["X-CSRF-Token"] = csrf
        self.assertEqual(self.a.post("/api/v1/chat/sessions", json={}, headers={"Origin": "https://evil.invalid"}).status_code, 403)
        self.assertEqual(self.a.post("/api/v1/auth/logout").status_code, 200)
        self.a.cookies.set(auth.settings.auth_cookie_name, raw)
        self.assertEqual(self.a.get("/api/v1/auth/me").status_code, 401)
        r = self.b.post("/api/v1/auth/login", json={"username": "ALICE", "password": "password-test-123"})
        self.assertEqual(r.status_code, 200, r.text)
        with self.sessions() as db:
            for row in db.scalars(select(models.AuthSession)):
                row.expires_at = models.utcnow() - timedelta(seconds=1)
            db.commit()
        self.assertEqual(self.b.get("/api/v1/auth/me").status_code, 401)

    def test_bad_credentials_duplicate_redaction_and_rate_limit(self):
        self.register(self.a, "alice")
        duplicate = {"username": "ALICE", "password": "password-test-123", "confirm_password": "password-test-123", "email": "new@example.com", "email_code": "000000"}
        self.assertEqual(self.b.post("/api/v1/auth/register", json=duplicate).status_code, 409)
        self.assertEqual(self.b.post("/api/v1/auth/login", json={"username": "alice", "password": "bad"}).status_code, 401)
        r = self.b.post("/api/v1/auth/register", json={"username": "other", "password": "sensitive"})
        self.assertEqual(r.status_code, 422)
        self.assertNotIn("sensitive", r.text)
        from dataclasses import replace
        with patch.object(auth_service, "settings", replace(auth_service.settings, auth_rate_limit=1)):
            self.assertEqual(self.b.post("/api/v1/auth/login", json={"username": "alice", "password": "bad"}).status_code, 429)

    def test_revoked_during_stream_does_not_save_answer(self):
        self.register(self.a, "alice")
        sid = self.a.post("/api/v1/chat/sessions", json={}).json()["id"]
        def generation(*args, **kwargs):
            yield {"type": "delta", "content": "partial"}
            with self.sessions() as db:
                db.query(models.AuthSession).delete()
                db.commit()
        with patch.object(chat_service, "run_agent", side_effect=generation):
            r = self.a.post(f"/api/v1/chat/sessions/{sid}/messages", json={"content": "hello"})
        self.assertIn('"status_code": 401', r.text)
        with self.sessions() as db:
            self.assertEqual([m.role for m in db.scalars(select(models.Message))], ["user"])

    def test_owned_stream_saves_history_and_title(self):
        self.register(self.a, "alice")
        sid = self.a.post("/api/v1/chat/sessions", json={}).json()["id"]
        with patch.object(chat_service, "run_agent", return_value=iter([{"type": "delta", "content": "complete answer"}])), \
             patch.object(chat_service.session_service, "generate_title", return_value="Test title"):
            r = self.a.post(f"/api/v1/chat/sessions/{sid}/messages", json={"content": "hello"})
        self.assertIn('"status": "complete"', r.text)
        history = self.a.get(f"/api/v1/chat/sessions/{sid}/messages").json()
        self.assertEqual([m["content"] for m in history], ["hello", "complete answer"])
        self.assertEqual(self.a.get("/api/v1/chat/sessions").json()[0]["title"], "Test title")

    def test_login_rotates_cookie_and_disabled_account_is_rejected(self):
        self.register(self.a, "alice")
        old = self.a.cookies.get(auth.settings.auth_cookie_name)
        r = self.a.post("/api/v1/auth/login", json={"username": "alice", "password": "password-test-123"})
        self.assertEqual(r.status_code, 200)
        self.assertNotEqual(old, self.a.cookies.get(auth.settings.auth_cookie_name))
        self.b.cookies.set(auth.settings.auth_cookie_name, old)
        self.assertEqual(self.b.get("/api/v1/auth/me").status_code, 401)
        with self.sessions() as db:
            db.scalar(select(models.User)).is_active = False
            db.commit()
        self.assertEqual(self.a.get("/api/v1/auth/me").status_code, 401)

    def test_local_assignment_preview_apply_and_no_overwrite(self):
        self.register(self.a, "alice")
        with self.sessions() as db:
            db.add(models.ChatSession(id="legacy", character_id=1, title="kept"))
            db.commit()
        env = {**os.environ, "DATABASE_URL": str(self.engine.url), "LOG_LEVEL": "WARNING"}
        cmd = [sys.executable, str(Path(__file__).resolve().parents[1] / "scripts/manage-accounts.py"), "assign", "--username", "alice", "--session-id", "legacy"]
        preview = subprocess.run(cmd, env=env, capture_output=True, text=True)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertEqual(self.a.get("/api/v1/chat/sessions").json(), [])
        applied = subprocess.run(cmd + ["--apply"], env=env, capture_output=True, text=True)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(self.a.get("/api/v1/chat/sessions").json()[0]["id"], "legacy")
        repeat = subprocess.run(cmd + ["--apply"], env=env, capture_output=True, text=True)
        self.assertNotEqual(repeat.returncode, 0)

    def test_email_code_success_uniqueness_and_single_use(self):
        body = self.registration_body(self.a, "alice")
        with self.sessions() as db:
            row = db.scalar(select(models.EmailVerification))
            self.assertNotEqual(row.code_hash, body["email_code"])
            self.assertNotIn(body["email_code"], row.code_hash)
        self.assertEqual(self.a.post("/api/v1/auth/register", json=body).status_code, 201)
        with self.sessions() as db:
            user = db.scalar(select(models.User))
            self.assertEqual(user.email, "alice@example.com")
            self.assertIsNotNone(user.email_verified_at)
            self.assertIsNone(db.scalar(select(models.EmailVerification)))
        body["username"] = "other"
        body["email"] = "ALICE@example.com"
        self.assertEqual(self.b.post("/api/v1/auth/register", json=body).status_code, 409)
        with patch.object(mail_service, "send_registration_code") as send:
            r = self.b.post("/api/v1/auth/email-code", json={"email": "alice@example.com"})
            self.assertEqual(r.status_code, 200)
            send.assert_not_called()

    def test_code_expiry_wrong_address_cooldown_and_attempt_lockout(self):
        body = self.registration_body(self.a, "alice")
        self.assertEqual(self.a.post("/api/v1/auth/email-code", json={"email": body["email"]}).status_code, 429)
        other = {**body, "email": "different@example.com"}
        self.assertEqual(self.a.post("/api/v1/auth/register", json=other).status_code, 400)
        wrong = {**body, "email_code": "000000" if body["email_code"] != "000000" else "111111"}
        for _ in range(5):
            self.assertEqual(self.a.post("/api/v1/auth/register", json=wrong).status_code, 400)
        self.assertEqual(self.a.post("/api/v1/auth/register", json=body).status_code, 400)
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(models.EmailVerification)).attempts, 5)
            self.assertEqual(len(db.scalars(select(models.User)).all()), 0)
        body = self.registration_body(self.b, "bob")
        with self.sessions() as db:
            row = db.get(models.EmailVerification, body["email"])
            row.expires_at = models.utcnow() - timedelta(seconds=1)
            db.commit()
        self.assertEqual(self.b.post("/api/v1/auth/register", json=body).status_code, 400)

    def test_resend_invalidates_previous_code_and_transport_failure_cannot_verify(self):
        body = self.registration_body(self.a, "alice")
        with self.sessions() as db:
            db.get(models.EmailVerification, body["email"]).resend_after = models.utcnow() - timedelta(seconds=1)
            db.commit()
        old = body["email_code"]
        # Ensure deterministic distinct codes in this resend test.
        replacement = 123456 if old != "123456" else 234567
        with patch.object(email_verification.secrets, "randbelow", return_value=replacement):
            self.assertEqual(self.a.post("/api/v1/auth/email-code", json={"email": body["email"]}).status_code, 200)
        self.assertEqual(self.a.post("/api/v1/auth/register", json=body).status_code, 400)
        body["email_code"] = self.deliveries[body["email"]]
        self.assertEqual(self.a.post("/api/v1/auth/register", json=body).status_code, 201)
        from fastapi import HTTPException
        with patch.object(mail_service, "send_registration_code", side_effect=HTTPException(503, "unavailable")):
            self.assertEqual(self.b.post("/api/v1/auth/email-code", json={"email": "broken@example.com"}).status_code, 503)
        with self.sessions() as db:
            self.assertFalse(db.get(models.EmailVerification, "broken@example.com").delivered)

    def test_password_rules_and_confirmation_are_server_enforced(self):
        body = self.registration_body(self.a, "alice")
        for password in ["abcdefghijk", "123456789012", "password123", " abcde12345", "abcde12345 "]:
            invalid = {**body, "password": password, "confirm_password": password}
            r = self.a.post("/api/v1/auth/register", json=invalid)
            self.assertEqual(r.status_code, 422, password)
            self.assertNotIn(password, r.text)
        self.assertEqual(self.a.post("/api/v1/auth/register", json={**body, "confirm_password": "something-else-123"}).status_code, 422)
        self.assertEqual(self.a.post("/api/v1/auth/register", json=body).status_code, 201)

    def test_empty_sessions_reused_across_clicks_tabs_and_after_reload(self):
        self.register(self.a, "alice")
        first = self.a.post("/api/v1/chat/sessions", json={}).json()
        for _ in range(3):
            self.assertEqual(self.a.post("/api/v1/chat/sessions", json={}).json()["id"], first["id"])
        self.assertFalse(self.a.get("/api/v1/chat/sessions").json()[0]["has_user_message"])
        # Two independent DB connections mimic different workers/tabs.
        from app.services import session_service
        with self.sessions() as db:
            uid = db.scalar(select(models.User.id))
        def create(_):
            with self.sessions() as db:
                return session_service.create_session(db, owner_id=uid).id
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(set(pool.map(create, range(8))), {first["id"]})
        self.assertEqual(self.a.post(f"/api/v1/chat/sessions/{first['id']}/messages", json={"content": "   "}).status_code, 422)
        with patch.object(chat_service, "run_agent", return_value=iter([{"type": "delta", "content": "answer"}])), \
             patch.object(chat_service.session_service, "generate_title", return_value="Title"):
            self.a.post(f"/api/v1/chat/sessions/{first['id']}/messages", json={"content": "hello"})
        second = self.a.post("/api/v1/chat/sessions", json={}).json()
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(self.a.get("/api/v1/chat/sessions").json()), 2)

    def test_concurrent_first_creation_makes_exactly_one_empty_session(self):
        self.register(self.a, "alice")
        from app.services import session_service
        with self.sessions() as db:
            uid = db.scalar(select(models.User.id))
        def create(_):
            with self.sessions() as db:
                return session_service.create_session(db, owner_id=uid).id
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(len(set(pool.map(create, range(8)))), 1)
        self.assertEqual(len(self.a.get("/api/v1/chat/sessions").json()), 1)

    def test_missing_mail_configuration_fails_closed_without_issuing_code(self):
        from fastapi import HTTPException
        with patch.object(mail_service, "require_mail", side_effect=HTTPException(503, "not configured")), \
             patch.object(mail_service, "send_registration_code") as send:
            self.assertEqual(self.a.post("/api/v1/auth/email-code", json={"email": "alice@example.com"}).status_code, 503)
            send.assert_not_called()
        with self.sessions() as db:
            self.assertIsNone(db.scalar(select(models.EmailVerification)))

    def test_verification_code_can_only_be_consumed_once(self):
        body = self.registration_body(self.a, "alice")
        with self.sessions() as db:
            nonce = email_verification.check_code(db, body["email"], body["email_code"])
            email_verification.consume_code(db, body["email"], nonce)
            db.commit()
        from fastapi import HTTPException
        with self.sessions() as db, self.assertRaises(HTTPException):
            email_verification.consume_code(db, body["email"], nonce)


class MigrationTests(unittest.TestCase):
    def test_legacy_preserved_backup_idempotency_and_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = create_engine("sqlite:///" + (Path(folder) / "app.db").as_posix())
            event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
            migrate(engine)
            migrate(engine, "0001_baseline")
            with engine.begin() as conn:
                conn.execute(text("DROP TABLE alembic_version"))
                conn.execute(text("INSERT INTO characters VALUES(1,'test','Test','Test','1','','','system','',1,CURRENT_TIMESTAMP)"))
                conn.execute(text("INSERT INTO chat_sessions VALUES('legacy',1,'kept',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
                conn.execute(text("INSERT INTO messages VALUES(1,'legacy','user','secret',CURRENT_TIMESTAMP)"))
            result = migrate(engine)
            self.assertTrue(Path(result["backup"]).is_file())
            self.assertFalse(migrate(engine)["changed"])
            with engine.connect() as conn:
                self.assertIsNone(conn.scalar(text("SELECT owner_id FROM chat_sessions")))
                self.assertEqual(conn.scalar(text("SELECT content FROM messages")), "secret")
                self.assertEqual(conn.scalar(text("SELECT has_user_message FROM chat_sessions")), 1)
                self.assertEqual(conn.execute(text("PRAGMA foreign_key_check")).all(), [])
            migrate(engine, "0001_baseline")
            migrate(engine)
            self.assertIn("users", inspect(engine).get_table_names())
            engine.dispose()

    def test_refuses_knowledge_database(self):
        engine = create_engine("sqlite://")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE knowledge_sources(id INTEGER)"))
        with self.assertRaises(RuntimeError):
            migrate(engine)
        self.assertEqual(inspect(engine).get_table_names(), ["knowledge_sources"])
        engine.dispose()


class MailTransportTests(unittest.TestCase):
    def test_ssl_smtp_and_redacted_failure(self):
        import smtplib
        from fastapi import HTTPException
        config = replace(mail_service.settings, smtp_host="smtp.qq.com", smtp_port=465, smtp_username="test@example.com",
                         smtp_from="test@example.com", smtp_password="private-app-password", smtp_security="ssl", email_code_secret="s" * 32)
        with patch.object(mail_service, "settings", config), patch.object(mail_service.smtplib, "SMTP_SSL") as smtp:
            smtp.return_value.send_message.return_value = {}
            mail_service.send_registration_code("reader@example.com", "123456")
            smtp.assert_called_once()
            client = smtp.return_value
            client.login.assert_called_once_with("test@example.com", "private-app-password")
            message = client.send_message.call_args.args[0]
            self.assertEqual(message["To"], "reader@example.com")
            self.assertIn("123456", message.get_content())
        with patch.object(mail_service, "settings", config), \
             patch.object(mail_service.smtplib, "SMTP_SSL", side_effect=smtplib.SMTPAuthenticationError(535, b"private-app-password")):
            with self.assertRaises(HTTPException) as result:
                mail_service.send_registration_code("reader@example.com", "123456")
            self.assertEqual(result.exception.status_code, 503)
            self.assertNotIn("private-app-password", result.exception.detail)


if __name__ == "__main__":
    unittest.main()
