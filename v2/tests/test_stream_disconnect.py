"""Real localhost TCP disconnects; isolated accounts/ledger and fake provider only."""
import json
from pathlib import Path
import socket
import sys
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import httpx
import uvicorn
from sqlalchemy import select
from app import llm, models
from app.services import usage_service as usage, session_service
from app.agents import intent
import test_auth as fixtures


class BlockingStream:
    def __init__(self, *, first_token=True, reported=False, complete=False):
        self.first_token = first_token
        self.reported = reported
        self.complete = complete
        self.reading = threading.Event()
        self.closed = threading.Event()
        self.close_count = 0

    def __iter__(self):
        self.reading.set()
        if self.reported:
            yield NS(usage=NS(model_dump=lambda: {'prompt_tokens': 100, 'completion_tokens': 10}), choices=[])
        if self.first_token:
            yield NS(usage=None, choices=[NS(finish_reason=None, delta=NS(content='partial', tool_calls=None))])
        if self.complete:
            yield NS(usage=None, choices=[NS(finish_reason='stop', delta=NS(content=None, tool_calls=None))])
            return
        if not self.closed.wait(10):
            raise RuntimeError('test provider was not closed on disconnect')

    def close(self):
        self.close_count += 1
        self.closed.set()


class DisconnectHTTPTests(unittest.TestCase):
    register = fixtures.AuthTests.register
    registration_body = fixtures.AuthTests.registration_body

    def setUp(self):
        fixtures.AuthTests.setUp(self)
        self.addCleanup(fixtures.AuthTests.tearDown, self)
        self.register(self.a, 'alice')
        self.sid = self.a.post('/api/v1/chat/sessions', json={}).json()['id']
        self.client = httpx.Client(timeout=5, trust_env=False, cookies=dict(self.a.cookies), headers={'X-CSRF-Token': self.a.headers['X-CSRF-Token']})
        self.addCleanup(self.client.close)
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        self.addCleanup(listener.close)
        self.base = 'http://127.0.0.1:' + str(listener.getsockname()[1])
        # Production routers/auth/SSE response, but no production bootstrap or DB.
        self.server = uvicorn.Server(uvicorn.Config(fixtures.app, lifespan='off', log_level='error', loop='asyncio'))
        self.thread = threading.Thread(target=self.server.run, kwargs={'sockets': [listener]}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        deadline = time.monotonic() + 5
        while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
            threading.Event().wait(.01)
        self.assertTrue(self.server.started)
        self.patchers = [patch.object(intent, 'resolve_intent', return_value=None), patch.object(session_service, 'generate_title', return_value='test')]
        self.original_title = session_service.generate_title
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def stop_server(self):
        self.server.should_exit = True
        self.thread.join(8)
        self.assertFalse(self.thread.is_alive(), 'ASGI request/worker did not exit')

    def wait_terminal(self, client_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self.sessions() as db:
                row = db.scalar(select(models.GenerationRequest).where(models.GenerationRequest.client_request_id == client_id))
                if row and row.status != 'running':
                    return row
            threading.Event().wait(.02)
        self.fail('disconnect did not settle the request promptly')

    def send(self, client_id):
        return self.client.stream('POST', self.base + f'/api/v1/chat/sessions/{self.sid}/messages', json={'content': 'hello', 'client_request_id': client_id})

    def exercise_disconnect(self, *, first_token, reported):
        upstream = BlockingStream(first_token=first_token, reported=reported)
        client = Mock()
        client.with_options.return_value = client
        client.chat.completions.create.return_value = upstream
        request_id = 'disconnect-request-00001'
        with patch.object(llm, '_get_client', return_value=client):
            with self.send(request_id) as response:
                self.assertEqual(response.status_code, 200)
                self.assertTrue(upstream.reading.wait(3))
                if first_token:
                    self.assertTrue(any('partial' in line for line in response.iter_lines() if line.startswith('data:')))
            self.assertTrue(upstream.closed.wait(3), 'TCP close did not close the provider stream')
            row = self.wait_terminal(request_id)
        self.assertEqual(row.status, 'cancelled')
        self.assertIsNone(row.assistant_message_id)
        self.assertIsNotNone(row.user_message_id)
        self.assertEqual(upstream.close_count, 1)
        self.assertEqual(client.chat.completions.create.call_count, 1)
        with self.sessions() as db:
            call = db.scalar(select(models.LLMUsage).where(models.LLMUsage.request_id == row.id))
            self.assertEqual(call.status, 'reported' if reported else 'estimated_unknown')
            self.assertGreater(call.charged_micros, 0)
            for key in json.loads(call.buckets):
                self.assertEqual(db.get(models.QuotaBucket, key).reserved_micros, 0)
            self.assertEqual(db.scalars(select(models.Message).where(models.Message.session_id == self.sid, models.Message.role == 'assistant')).all(), [])
        # Admission slot is usable immediately, rather than after the lease expires.
        next_scope, fresh = usage.admit(self.sessions, row.owner_id, self.sid, 'next-request-after-disconnect', 'hello again')
        self.assertTrue(fresh)
        usage.finish(usage.Scope(next_scope['id'], self.sessions), 'failed')

    def test_disconnect_during_first_token_wait_closes_upstream_and_settles(self):
        self.exercise_disconnect(first_token=False, reported=False)

    def test_disconnect_after_delta_preserves_reported_usage(self):
        self.exercise_disconnect(first_token=True, reported=True)

    def test_normal_completion_and_duplicate_replay_do_not_charge_again(self):
        upstream = BlockingStream(reported=True, complete=True)
        client = Mock()
        client.with_options.return_value = client
        client.chat.completions.create.return_value = upstream
        with patch.object(llm, '_get_client', return_value=client):
            with self.send('normal-http-request-00001') as response:
                text = response.read().decode()
                self.assertIn('"type": "done"', text)
            row = self.wait_terminal('normal-http-request-00001')
            with self.send('normal-http-request-00001') as response:
                self.assertIn('partial', response.read().decode())
        self.assertEqual(row.status, 'completed')
        self.assertEqual(client.chat.completions.create.call_count, 1)
        self.assertEqual(upstream.close_count, 1)

    def test_disconnect_during_short_call_stops_followup_generation(self):
        entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
        original_cancel = usage.Scope.cancel

        def cancel(scope):
            original_cancel(scope)
            cancelled.set()

        def create(**kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test short call was not released')
            return NS(usage={'prompt_tokens': 100, 'completion_tokens': 10})

        def parse(messages):
            llm.completion(purpose='intent', model='deepseek-flash', max_tokens=128, messages=messages)
            return None

        client = Mock()
        client.with_options.return_value = client
        client.chat.completions.create.side_effect = create
        with patch.object(llm, '_get_client', return_value=client), patch.object(intent, 'resolve_intent', side_effect=parse), patch.object(usage.Scope, 'cancel', cancel):
            try:
                with self.send('disconnect-short-call-00001') as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(entered.wait(3))
                self.assertTrue(cancelled.wait(3))
            finally:
                release.set()
            row = self.wait_terminal('disconnect-short-call-00001')
        self.assertEqual(row.status, 'cancelled')
        self.assertIsNone(row.assistant_message_id)
        self.assertEqual(client.chat.completions.create.call_count, 1)
        with self.sessions() as db:
            call = db.scalar(select(models.LLMUsage).where(models.LLMUsage.request_id == row.id))
            self.assertEqual(call.status, 'reported')
            self.assertTrue(all(db.get(models.QuotaBucket, key).reserved_micros == 0 for key in json.loads(call.buckets)))

    def test_disconnect_during_title_preserves_completed_answer_and_accounting(self):
        entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
        original_cancel = usage.Scope.cancel
        upstream = BlockingStream(reported=True, complete=True)

        def cancel(scope):
            original_cancel(scope)
            cancelled.set()

        def create(**kwargs):
            if kwargs.get('stream'):
                return upstream
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test title was not released')
            return NS(usage={'prompt_tokens': 100, 'completion_tokens': 10}, choices=[NS(finish_reason='stop', message=NS(content='late title'))])

        client = Mock()
        client.with_options.return_value = client
        client.chat.completions.create.side_effect = create
        with patch.object(llm, '_get_client', return_value=client), patch.object(session_service, 'generate_title', self.original_title), patch.object(usage.Scope, 'cancel', cancel):
            try:
                with self.send('disconnect-title-call-00001') as response:
                    lines = response.iter_lines()
                    for line in lines:
                        if line.startswith('data:') and json.loads(line[5:])['type'] == 'done':
                            break
                    self.assertTrue(entered.wait(3))
                self.assertTrue(cancelled.wait(3))
            finally:
                release.set()
            row = self.wait_terminal('disconnect-title-call-00001')
        self.assertEqual(row.status, 'completed')
        self.assertIsNotNone(row.assistant_message_id)
        with self.sessions() as db:
            self.assertEqual(db.get(models.Message, row.assistant_message_id).content, 'partial')
            self.assertNotEqual(db.get(models.ChatSession, self.sid).title, 'late title')
            calls = db.scalars(select(models.LLMUsage).where(models.LLMUsage.request_id == row.id)).all()
            self.assertEqual({call.purpose for call in calls}, {'answer', 'title'})
            self.assertTrue(all(call.status == 'reported' for call in calls))
            self.assertTrue(all(db.get(models.QuotaBucket, key).reserved_micros == 0 for call in calls for key in json.loads(call.buckets)))
        self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_disconnect_between_answer_commit_and_done_retains_replay(self):
        saved, release, cancelled = threading.Event(), threading.Event(), threading.Event()
        original_cancel = usage.Scope.cancel
        original_append = session_service.append_message

        def cancel(scope):
            original_cancel(scope)
            cancelled.set()

        def append(db, session_id, role, content, **kwargs):
            row = original_append(db, session_id, role, content, **kwargs)
            if role == 'assistant':
                saved.set()
                if not release.wait(5):
                    raise RuntimeError('test commit gate was not released')
            return row

        client = Mock()
        client.with_options.return_value = client
        client.chat.completions.create.return_value = BlockingStream(reported=True, complete=True)
        request_id = 'disconnect-before-done-00001'
        with patch.object(llm, '_get_client', return_value=client), patch.object(session_service, 'append_message', side_effect=append), patch.object(usage.Scope, 'cancel', cancel):
            try:
                with self.send(request_id) as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(saved.wait(3))
                self.assertTrue(cancelled.wait(3))
            finally:
                release.set()
            row = self.wait_terminal(request_id)
            self.assertEqual(row.status, 'completed')
            self.assertIsNotNone(row.assistant_message_id)
            with self.send(request_id) as response:
                self.assertIn('partial', response.read().decode())
        self.assertEqual(client.chat.completions.create.call_count, 1)


if __name__ == '__main__':
    unittest.main()
