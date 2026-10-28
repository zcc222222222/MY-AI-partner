"""Real browser -> local HTTP -> isolated accounts -> real read-only job; no external models."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))


def serve(port):
    marker=Path(os.environ.get('AIPARTNER_OPS_FIXTURE','')).resolve()
    if (not marker.name.startswith('aipartner-ops-') or not marker.is_relative_to(Path(tempfile.gettempdir()).resolve())
            or not (marker/'.fixture').is_file()):
        raise RuntimeError('Fixture server requires a dedicated temporary directory')
    expected='sqlite:///'+(marker/'app.db').as_posix()
    if os.environ.get('DATABASE_URL')!=expected or Path(os.environ.get('KB_DATA_DIR','')).resolve()!=marker:
        raise RuntimeError('Fixture database configuration does not match temporary directory')
    from app.main import app
    from app.database import engine,SessionLocal
    from app.migrations import migrate
    from app.models import User
    from app.services.auth_service import passwords
    from app.services import chat_service,session_service
    migrate(engine)
    with SessionLocal() as db:
        for uid,name in ((1,'ops_admin'),(2,'ops_player')):
            if not db.get(User,uid):
                db.add(User(id=uid,username=name,display_name=name,password_hash=passwords.hash('FixtureOnly12345!'),is_active=True))
        db.commit()
    def echo(*args,**kwargs):
        yield {'type':'delta','content':'本地联调回显成功。'}
    chat_service.run_agent=echo
    session_service.generate_title=lambda text:'联调会话'
    from app.services import retrieval_diagnostics as diagnostics
    from app.agents import live_guides
    def timed_out():
        diagnostics.observe(reason='timeout',stage='search_response',cache='network')
        return {'status':'source_unavailable','retry_after_seconds':30}
    diagnostics.run('find_gameplay_guides',{'query':'私密测试问题，深塔'},timed_out)
    def fixture_guide(query):
        assert diagnostics.FORCE_REFRESH.get()
        diagnostics.observe(cache='network',stage='search_response')
        return {'status':'links_found','results':[{'images':[{},{}]}]}
    live_guides.find_gameplay_guides=fixture_guide
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=port,log_level='warning')


def backup(source,target):
    target.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True)) as original, closing(sqlite3.connect(target)) as copied:
        original.backup(copied)


def main(output):
    from app.config import settings  # Load local configuration without printing it.
    from knowledge.config import DATA_DIR
    from playwright.sync_api import sync_playwright
    source=Path(DATA_DIR)
    output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='aipartner-ops-') as folder:
        fixture=Path(folder);(fixture/'.fixture').write_text('isolated',encoding='utf-8')
        backup(source/'wiki/wiki.db',fixture/'wiki/wiki.db')
        backup(source/'knowledge.db',fixture/'knowledge.db')
        digest=hashlib.sha256((fixture/'wiki/wiki.db').read_bytes()).hexdigest()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        env={**os.environ,'AIPARTNER_OPS_FIXTURE':str(fixture),'KB_DATA_DIR':str(fixture),
             'RETRIEVAL_DIAGNOSTICS_DIR':str(fixture/'diagnostics'),
             'DATABASE_URL':'sqlite:///'+(fixture/'app.db').as_posix(),'KNOWLEDGE_ADMIN_USER_IDS':'1',
             'DEEPSEEK_API_KEY':'fixture-no-model-key-at-least-32-characters',
             'DEEPSEEK_BASE_URL':'http://127.0.0.1:1','SMTP_HOST':'','SMTP_USERNAME':'','SMTP_PASSWORD':'','SMTP_FROM':''}
        base=f'http://127.0.0.1:{port}'
        with (output/'fixture-server.log').open('wb') as log:
            server=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--serve-fixture','--port',str(port)],
                cwd=str(ROOT/'backend'),env=env,stdout=log,stderr=log,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            deadline=time.monotonic()+40
            while True:
                try:
                    with urlopen(base+'/openapi.json',timeout=1):break
                except OSError:
                    if time.monotonic()>deadline or server.poll() is not None:raise RuntimeError('Fixture server unavailable')
                    time.sleep(.2)
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True)
                admin=browser.new_context(viewport={'width':1360,'height':1000})
                admin.route('**/*',lambda route:route.abort() if route.request.resource_type in ('media','font') else route.continue_())
                page=admin.new_page();page.goto(base,wait_until='domcontentloaded')
                page.get_by_label('用户名',exact=True).fill('ops_admin')
                page.get_by_label('密码',exact=True).fill('FixtureOnly12345!')
                page.get_by_role('button',name='登录',exact=True).click()
                page.get_by_role('button',name='知识库管理',exact=True).click(timeout=15000)
                page.get_by_role('heading',name='来源检查',exact=True).wait_for(timeout=20000)
                response=admin.request.get(base+'/api/v1/knowledge/ops')
                assert response.status==200 and response.headers.get('cache-control')=='no-store'
                assert admin.request.post(base+'/api/v1/knowledge/ops/jobs',data={'recipe':'inspect'}).status==403
                assert admin.request.post(base+'/api/v1/knowledge/ops/retrievals/recheck',data={'topic':'逆境深塔'}).status==403
                page.get_by_role('heading',name='最近检索记录',exact=True).scroll_into_view_if_needed()
                assert '私密测试问题' not in page.locator('.knowledge-panel').inner_text()
                page.get_by_role('button',name='重新检查所选玩法',exact=True).click()
                page.get_by_role('heading',name='逆境深塔 · 取得结果',exact=True).wait_for(timeout=15000)
                records=admin.request.get(base+'/api/v1/knowledge/ops/retrievals').json()['records']
                assert records[0]['reason']=='success' and records[0]['image_count']==2
                assert any(r.get('reason')=='timeout' for r in records)
                page.screenshot(path=str(output/'retrieval-records.png'))
                page.screenshot(path=str(output/'overview-desktop.png'))
                page.get_by_role('button',name='检查本地计划',exact=True).click()
                page.get_by_role('heading',name='检查本地更新计划 · 流程完成',exact=True).wait_for(timeout=30000)
                job=admin.request.get(base+'/api/v1/knowledge/ops').json()['jobs'][0]
                assert job['state']=='completed' and job['recipe']=='inspect'
                page.locator('.ops-job').first.scroll_into_view_if_needed()
                page.screenshot(path=str(output/'job-completed.png'))
                page.set_viewport_size({'width':390,'height':844})
                page.locator('.knowledge-panel').evaluate('(d)=>d.scrollTo(0,0)')
                assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
                page.screenshot(path=str(output/'overview-mobile.png'))
                page.get_by_role('button',name='返回聊天',exact=True).click()
                # A separate ordinary account has neither the entry point nor the HTTP capability.
                normal=browser.new_context();other=normal.new_page();other.goto(base,wait_until='domcontentloaded')
                other.get_by_label('用户名',exact=True).fill('ops_player')
                other.get_by_label('密码',exact=True).fill('FixtureOnly12345!')
                other.get_by_role('button',name='登录',exact=True).click()
                other.locator('.sidebar').wait_for(timeout=15000)
                assert other.get_by_role('button',name='知识库管理',exact=True).count()==0
                assert normal.request.get(base+'/api/v1/knowledge/ops').status==403
                assert normal.request.get(base+'/api/v1/knowledge/ops/retrievals').status==403
                anonymous=browser.new_context()
                assert anonymous.request.get(base+'/api/v1/knowledge/ops').status==401
                assert anonymous.request.get(base+'/api/v1/knowledge/ops/retrievals').status==401
                anonymous.close();normal.close();admin.close();browser.close()
            assert hashlib.sha256((fixture/'wiki/wiki.db').read_bytes()).hexdigest()==digest
            report={'status':'passed','admin_entry':True,'ordinary_account_denied':True,'anonymous_denied':True,
                    'retrieval_recheck_completed':True,'retrieval_privacy':True,
                    'csrf_enforced':True,'real_read_only_worker_completed':True,'wiki_unchanged':True,
                    'mobile_no_horizontal_overflow':True,'external_models':0,'external_collection':0,
                    'formal_account_database_used':False}
            (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(report,ensure_ascii=True))
        finally:
            server.terminate()
            try:server.wait(timeout=15)
            except subprocess.TimeoutExpired:server.kill();server.wait(timeout=5)


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--serve-fixture',action='store_true',help=argparse.SUPPRESS)
    ap.add_argument('--port',type=int,default=0,help=argparse.SUPPRESS)
    ap.add_argument('--output',type=Path,default=ROOT/'backend/data/evaluations/2026-10-26-knowledge-ops')
    args=ap.parse_args()
    if args.serve_fixture:serve(args.port)
    else:main(args.output.resolve())
