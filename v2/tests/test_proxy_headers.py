"""Exercise the deployed Uvicorn middleware and real Origin checks offline."""
from dataclasses import replace
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from app import auth


class ProxyHeadersTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()

        @app.post('/probe')
        def probe(request: Request):
            auth.check_origin(request)
            return {'scheme': request.url.scheme, 'client': request.client.host}

        self.app = ProxyHeadersMiddleware(app, trusted_hosts='172.29.0.1')
        self.settings = patch.object(auth, 'settings', replace(auth.settings, cors_origins=[]))
        self.settings.start()
        self.addCleanup(self.settings.stop)

    def request(self, peer='172.29.0.1', origin='https://demo.example.com', ip='203.0.113.10', **extra):
        headers = {'Origin': origin, 'X-Forwarded-Proto': 'https', 'X-Forwarded-For': ip, **extra}
        with TestClient(self.app, base_url='http://demo.example.com', client=(peer, 1234)) as client:
            return client.post('/probe', headers=headers)

    def test_trusted_proxy_restores_https_same_origin(self):
        result = self.request()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), {'scheme': 'https', 'client': '203.0.113.10'})

    def test_trusted_proxy_keeps_distinct_client_ips(self):
        self.assertNotEqual(self.request(ip='203.0.113.10').json()['client'],
                            self.request(ip='203.0.113.11').json()['client'])

    def test_untrusted_peer_cannot_claim_https(self):
        self.assertEqual(self.request(peer='198.51.100.7').status_code, 403)

    def test_untrusted_peer_cannot_forge_client_ip(self):
        result = self.request(peer='198.51.100.7', origin='http://demo.example.com')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), {'scheme': 'http', 'client': '198.51.100.7'})

    def test_trusted_proxy_does_not_allow_foreign_origin(self):
        self.assertEqual(self.request(origin='https://evil.example').status_code, 403)

    def test_forwarded_host_does_not_override_origin(self):
        self.assertEqual(self.request(origin='https://evil.example',
                                      **{'X-Forwarded-Host': 'evil.example'}).status_code, 403)
