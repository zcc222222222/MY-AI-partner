"""Exercise Compose with isolated data and a fake API key; never call a model."""
import argparse
from datetime import datetime, timezone
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener
import uuid

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-knowledge', action='store_true', help='Do not copy public knowledge snapshots')
    args = parser.parse_args()
    name = 'aipartner-check-' + uuid.uuid4().hex[:10]
    output = ROOT / 'v2/backend/data/container-evaluations' / name
    data = output / 'data'
    data.mkdir(parents=True)
    # Only this disposable fixture directory is shared with the non-root image.
    data.chmod(0o777)
    envfile = output / 'fixture.env'
    envfile.write_text('DEEPSEEK_API_KEY=container-smoke-only-not-a-provider-key\nAUTH_COOKIE_SECURE=false\nKNOWLEDGE_ADMIN_USER_IDS=\n', encoding='utf-8')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = {**os.environ, 'APP_ENV_FILE': str(envfile), 'APP_DATA_DIR': str(data),
           'APP_BIND_ADDRESS': '127.0.0.1', 'APP_PORT': str(port)}
    compose = ['docker', 'compose', '-p', name]
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'passed': False,
              'model_calls': 0, 'production_accounts_copied': False, 'checks': {}}

    def run(command, *, timeout=180):
        result = subprocess.run(command, cwd=ROOT, env=env, text=True, encoding='utf-8',
                                errors='replace', capture_output=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError('Command failed: ' + ' '.join(command[:5]) + '\n' + result.stderr[-4000:])
        return result.stdout

    def backup(source, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.parent.chmod(0o777)
        with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as src:
            with sqlite3.connect(destination) as dst:
                src.backup(dst)
        destination.chmod(0o666)

    copied = []
    if not args.skip_knowledge:
        for relative in ('knowledge.db', 'wiki/wiki.db'):
            source = ROOT / 'v2/backend/data' / relative
            if source.is_file():
                backup(source, data / relative)
                copied.append(relative)
    report['public_snapshots_copied'] = copied
    base = 'http://127.0.0.1:' + str(port)
    client = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))

    def request(path, payload=None, csrf=None):
        headers = {'Content-Type': 'application/json'}
        if csrf:
            headers['X-CSRF-Token'] = csrf
        req = Request(base + path, data=None if payload is None else json.dumps(payload).encode(), headers=headers)
        try:
            with client.open(req, timeout=10) as response:
                return response.status, response.read(), response.headers
        except HTTPError as exc:
            return exc.code, exc.read(), exc.headers

    try:
        run(compose + ['up', '-d', '--no-build', '--wait', '--wait-timeout', '120'])
        checks = report['checks']
        checks['health'] = request('/api/v1/health')[0] == 200
        status, body, _ = request('/')
        checks['frontend'] = status == 200 and b'<div id="app">' in body
        checks['private_api_denies_anonymous'] = request('/api/v1/chat/sessions')[0] == 401
        # Fixture users are created only in the isolated container's new app.db.
        run(compose + ['exec', '-T', 'app', 'python', '-c',
            "from app.database import SessionLocal; from app.models import User; "
            "from app.services.auth_service import passwords; "
            "db=SessionLocal(); db.add(User(username='docker_demo',display_name='Docker demo',"
            "password_hash=passwords.hash('Container-test-123'))); db.commit(); db.close()"])
        status, body, _ = request('/api/v1/auth/login', {'username': 'docker_demo', 'password': 'Container-test-123'})
        assert status == 200, 'Fixture login failed'
        csrf = json.loads(body)['csrf_token']
        checks['login'] = True
        checks['csrf'] = request('/api/v1/chat/sessions', {})[0] == 403
        status, body, _ = request('/api/v1/chat/sessions', {}, csrf)
        assert status == 200
        session_id = json.loads(body)['id']
        checks['quota'] = request('/api/v1/chat/quota')[0] == 200
        # Exercise Chromium and its system libraries under Compose's real user,
        # read-only filesystem, /tmp mount and capability restrictions.
        run(compose + ['exec', '-T', 'app', 'python', '-c',
            "import os,time; from pathlib import Path; from playwright.sync_api import sync_playwright; "
            "assert os.getuid()==10001; assert time.localtime().tm_gmtoff==28800; assert not Path('.env').exists(); "
            "p=sync_playwright().start(); b=p.chromium.launch(headless=True); page=b.new_page(); "
            "page.route('**/*',lambda r:r.continue_() if r.request.url.startswith('http://127.0.0.1:8000/') else r.abort()); "
            "page.goto('http://127.0.0.1:8000/',wait_until='networkidle'); "
            "assert page.locator('#app').inner_text().strip(); "
            "page.screenshot(path='/app/data/container-login.png'); b.close(); p.stop()"])
        checks['non_root_browser_and_ui'] = True
        image_env = json.loads(run(['docker', 'image', 'inspect', 'my-aipartner:local', '--format', '{{json .Config.Env}}']))
        checks['no_baked_api_key'] = not any(item.startswith('DEEPSEEK_API_KEY=') for item in image_env)
        if len(copied) == 2:
            result = json.loads(run(compose + ['exec', '-T', 'app', 'python', '-c',
                "import json; from llmwiki.tools import search_wiki,search_documents; "
                "a=search_wiki('技能机制','爱弥斯'); b=search_documents('配队',entity_name='爱弥斯',category='guide'); "
                "print(json.dumps({'mechanics':a.get('status'),'guide':b.get('status')}))"]))
            report['knowledge_result'] = result
            checks['relocated_public_knowledge'] = all(value == 'found' for value in result.values())
        run(compose + ['restart', 'app'])
        run(compose + ['up', '-d', '--no-build', '--wait', '--wait-timeout', '120'])
        status, body, _ = request('/api/v1/chat/sessions')
        checks['restart_persists_login_and_session'] = status == 200 and any(row['id'] == session_id for row in json.loads(body))
        report['passed'] = all(checks.values())
        if not report['passed']:
            raise RuntimeError('One or more container checks failed')
    except Exception as exc:
        report['error'] = str(exc)
        try:
            (output / 'container.log').write_text(run(compose + ['logs', '--no-color', '--tail', '100']), encoding='utf-8')
        except Exception:
            pass
    finally:
        try:
            run(compose + ['down', '--remove-orphans'])
            report['containers_stopped'] = True
        except Exception as exc:
            report['cleanup_error'] = str(exc)
            report['passed'] = False
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': report['passed'], 'report': str(output / 'report.json'), 'checks': report['checks']}, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
