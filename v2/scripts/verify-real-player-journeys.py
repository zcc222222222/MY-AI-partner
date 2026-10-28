"""Opt-in real model -> HTTP/SSE -> history and browser acceptance in isolated accounts."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
PASSWORD='JourneyFixture12345!'


def key(question): return hashlib.sha256(question.encode()).hexdigest()[:20]


def consume_event(item, line):
    if not line.startswith('data: '):return
    event=json.loads(line[6:])
    if event['type']=='delta':item['answer']+=event['content']
    elif event['type']=='replace':item['answer']=event['content']
    elif event['type']=='done':item.update(complete=True,completion_status=event.get('status'))
    elif event['type']=='error':item['transport_error']=event.get('detail')


def serve(port):
    fixture=Path(os.environ.get('AIPARTNER_JOURNEY_FIXTURE','')).resolve()
    if (not fixture.name.startswith('aipartner-journey-') or not fixture.is_relative_to(Path(tempfile.gettempdir()).resolve())
            or not (fixture/'.fixture').is_file() or os.environ.get('DATABASE_URL')!='sqlite:///'+(fixture/'app.db').as_posix()
            or Path(os.environ.get('KB_DATA_DIR','')).resolve()!=fixture):
        raise RuntimeError('A dedicated temporary fixture is required')
    from app.main import app
    from app.database import engine,SessionLocal
    from app.migrations import migrate
    from app.models import User
    from app.services.auth_service import passwords
    from app.services import chat_service,session_service,retrieval_diagnostics as diagnostics
    from app.agents import orchestrator,live_guides
    from llmwiki.updates import atomic_json
    migrate(engine)
    with SessionLocal() as db:
        for uid,name in ((1,'journey_admin'),(2,'journey_player')):
            db.add(User(id=uid,username=name,display_name=name,password_hash=passwords.hash(PASSWORD),is_active=True))
        db.commit()
    # Answer generation, intent parsing, tools and session memory remain real.
    config=json.loads((fixture/'acceptance.json').read_text(encoding='utf-8'))
    if not config.get('real_titles'):
        session_service.generate_title=lambda text:'验收 '+key(text)[:8]
    output=Path(config['output']);active=[None];serial=threading.Lock()
    original_execute=orchestrator.execute_tool
    original_agent=chat_service.run_agent
    original_community=live_guides.CLIENT.community
    def execute(name,args):
        result=original_execute(name,args)
        if active[0] is not None:
            active[0]['calls'].append({'name':name,'arguments':json.loads(args),'result':json.loads(result)})
        return result
    def community(keyword):
        mode=(active[0] or {}).get('fault')
        if mode:
            diagnostics.observe(reason=mode,stage='cooldown' if mode=='cooldown' else 'search_response',cache='none' if mode=='cooldown' else 'network')
            live_guides.CLIENT.next_at=time.monotonic()+(600 if mode=='cooldown' else 30)
            raise ValueError('Acceptance transport fault')
        return original_community(keyword)
    def agent(messages,**kwargs):
        # This fixture deliberately runs one real question at a time. A Lock can span SSE thread hops.
        with serial:
            question=next(m['content'] for m in reversed(messages) if m['role']=='user')
            captured={'question':question,'calls':[],'events':[],'fault':config['faults'].get(key(question))}
            active[0]=captured
            try:
                for event in original_agent(messages,**kwargs):
                    if event['type'] in ('knowledge_trace','intent_trace','tool_status','usage','generation'):
                        captured['events'].append(event)
                    yield event
            finally:
                atomic_json(output/'captures'/(key(question)+'.json'),captured)
                active[0]=None
    orchestrator.execute_tool=execute
    live_guides.CLIENT.community=community
    chat_service.run_agent=agent
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=port,log_level='warning')


def backup(source,target):
    target.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True)) as a,closing(sqlite3.connect(target)) as b:a.backup(b)


def groups():
    cases=json.loads((ROOT/'tests/player_questions.json').read_text(encoding='utf-8'))['cases']
    ids=('weapon_primary','echo_crit','team_both','team_missing','rotation','sanhua_role','rover_electro','rover_aero','rover_havoc','wrong_author','wrong_date')
    result=[{'id':'single/'+ident,'turns':[next(c for c in cases if c['id']==ident)]} for ident in ids]
    result.append({'id':'single/stats90','turns':[{'question':'爱弥斯90级的基础生命、攻击、防御各是多少？只给数值与来源。','required_tool':'get_character_stats'}]})
    result+=json.loads((ROOT/'tests/conversation_questions.json').read_text(encoding='utf-8'))['scenarios']
    result += [{'id':'live/'+topic,'turns':[{'question':'当期'+topic+'怎么配队？给我图文攻略。','live_topic':topic}]} for topic in ('逆境深塔','冥歌海墟','终焉矩阵')]
    result += [{'id':'fault/'+mode,'turns':[{'question':question,'fault':mode}]} for mode,question in
               (('timeout','当期深塔有没有可看的攻略？'),('cooldown','这期深塔的阵容图能发我吗？'))]
    return result


def judge(case,item,prior):
    errors=[];answer=item['answer'];capture=item.get('capture',{})
    traces=[e for e in capture.get('events',[]) if e['type']=='knowledge_trace']
    refs={s.get('source_id') for t in traces for s in t.get('sources',[]) if s.get('source_id')}
    if not item.get('complete'):errors.append('incomplete_sse')
    if answer!=item.get('stored_answer'):errors.append('stream_history_mismatch')
    if not answer.strip():errors.append('empty_answer')
    if item.get('completion_status')=='fallback':errors.append('generation_fallback')
    required=case.get('required_source') or case.get('source')
    if required and not case.get('empty') and required not in refs and required not in prior:errors.append('missing_required_source')
    allowed=case.get('allowed_sources') or ([case['source']] if case.get('source') else [])
    if allowed and refs-set(allowed):errors.append('wrong_source')
    if case.get('empty') and refs:errors.append('unverified_scope_source_delivered')
    if (case.get('image_mode')=='none' or case.get('images')=='none') and '![' in answer:errors.append('declined_images')
    for options in case.get('answer_need',[]):
        # Reviewed equivalent: "原文没标链数" expresses exactly the expected missing annotation.
        equivalent='未标' in options and '原文没标链数' in answer
        if not equivalent and not any(term in answer for term in options):errors.append('answer_term:'+str(options))
    if case.get('required_tool') and not any(c['name']==case['required_tool'] for c in capture.get('calls',[])):errors.append('missing_required_tool')
    if case.get('live_topic') or case.get('fault'):
        calls=[c for c in capture.get('calls',[]) if c['name']=='find_gameplay_guides']
        if not calls:errors.append('no_live_guide_call')
        item['availability']=[{'status':c['result'].get('status'),'reason':c['result'].get('failure_reason'),
                               'currency_verified':c['result'].get('currency_verified')} for c in calls]
        if case.get('live_topic'):
            item['external_content_available']=any(c['result'].get('currency_verified') is True and c['result'].get('status') not in ('source_unavailable','period_unverified','no_results') for c in calls)
        if case.get('fault'):
            if not any(c['result'].get('failure_reason')==case['fault'] for c in calls):errors.append('fault_not_observed')
            if '![' in answer:errors.append('fault_answer_has_unverified_images')
    item['errors']=sorted(set(errors));item['passed']=not errors
    return refs


def main(output,selected,real_titles=False):
    import httpx
    from app.config import settings
    from knowledge.config import DATA_DIR
    from playwright.sync_api import sync_playwright
    output.mkdir(parents=True,exist_ok=True)
    report={'schema':'real-player-journeys-v1','passed':False,'groups':[],
            'limits':'Real answer/intent models, real HTTP/SSE, temporary accounts and copied public knowledge. Titles use the real model only with --real-titles; otherwise they are deterministic. Only explicitly labelled timeout/cooldown cases inject transport faults. Source availability and answer checks are reported separately.'}
    chosen=[g for g in groups() if not selected or g['id'] in selected]
    if not chosen:raise ValueError('No selected groups')
    def save(): (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    with tempfile.TemporaryDirectory(prefix='aipartner-journey-') as folder:
        fixture=Path(folder);(fixture/'.fixture').write_text('isolated')
        for name in ('wiki/wiki.db','knowledge.db'):backup(Path(DATA_DIR)/name,fixture/name)
        if (Path(DATA_DIR)/'live-guide-cache').is_dir():shutil.copytree(Path(DATA_DIR)/'live-guide-cache',fixture/'live-guide-cache')
        faults={key(c['question']):c['fault'] for g in chosen for c in g['turns'] if c.get('fault')}
        (fixture/'acceptance.json').write_text(json.dumps({'output':str(output),'faults':faults,'real_titles':real_titles}),encoding='utf-8')
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        env={**os.environ,'AIPARTNER_JOURNEY_FIXTURE':str(fixture),'KB_DATA_DIR':str(fixture),
             'DATABASE_URL':'sqlite:///'+(fixture/'app.db').as_posix(),'RETRIEVAL_DIAGNOSTICS_DIR':str(fixture/'diagnostics'),
             'KNOWLEDGE_ADMIN_USER_IDS':'1','AUTH_COOKIE_SECURE':'false','DEEPSEEK_API_KEY':settings.deepseek_api_key,
             'DEEPSEEK_BASE_URL':settings.deepseek_base_url,'SMTP_HOST':'','SMTP_USERNAME':'','SMTP_PASSWORD':'','SMTP_FROM':''}
        base=f'http://127.0.0.1:{port}'
        with (output/'server.log').open('wb') as log:
            server=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--serve-fixture','--port',str(port)],cwd=str(ROOT/'backend'),
                env=env,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            deadline=time.monotonic()+45
            while True:
                try:
                    with urlopen(base+'/openapi.json',timeout=1):break
                except Exception:
                    if time.monotonic()>deadline:raise RuntimeError('Isolated server failed to start')
                    time.sleep(.3)
            def login(name):
                client=httpx.Client(base_url=base,timeout=240)
                r=client.post('/api/v1/auth/login',json={'username':name,'password':PASSWORD});r.raise_for_status()
                client.headers['X-CSRF-Token']=r.json()['csrf_token']
                return client
            admin=login('journey_admin');normal=login('journey_player')
            admin_sessions=[]
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True);context=browser.new_context(viewport={'width':1360,'height':1000})
                context.route('**/*',lambda r:r.abort() if r.request.resource_type in ('media','font') else r.continue_())
                page=context.new_page();page.goto(base,wait_until='domcontentloaded')
                # Chromium may not retain SSE response bodies for Response.body(). Clone only
                # the fixture's chat stream and let the actual application consume the original.
                page.evaluate('''() => {
                    const original = window.fetch.bind(window);
                    window.fetch = async (...args) => {
                        const response = await original(...args);
                        if (String(args[0]).includes('/messages') && args[1]?.method === 'POST') {
                            window.__journeyWire = null;
                            response.clone().text().then(text => { window.__journeyWire = text; });
                        }
                        return response;
                    };
                }''')
                page.get_by_label('用户名',exact=True).fill('journey_admin');page.get_by_label('密码',exact=True).fill(PASSWORD)
                page.get_by_role('button',name='登录',exact=True).click();page.locator('.sidebar').wait_for(timeout=15000)
                first=True
                for group in chosen:
                    scenario={'id':group['id'],'turns':[]};report['groups'].append(scenario);prior=set()
                    session_id=None
                    for case in group['turns']:
                        started=time.monotonic();item={'question':case['question'],'answer':'','complete':False};scenario['turns'].append(item)
                        try:
                            if first:
                                page.locator('textarea').fill(case['question'])
                                with page.expect_response(lambda r:'/messages' in r.url and r.request.method=='POST',timeout=20000) as sent:
                                    page.get_by_role('button',name='发送',exact=True).click()
                                page.locator('[data-od-id="stop-button"]').wait_for(state='hidden',timeout=240000)
                                session_id=page.locator('.session-id').inner_text()
                                # Capture actual streamed Markdown independently of the persisted history.
                                page.wait_for_function('typeof window.__journeyWire === "string"',timeout=240000)
                                for line in page.evaluate('window.__journeyWire').splitlines():consume_event(item,line)
                                item['browser_markdown_blocks']=page.locator('.message.assistant .md').last.locator('p,strong,ul,ol,table').count()
                                try:page.wait_for_function('Array.from(document.querySelectorAll(".message.assistant .md img")).every(i=>i.complete)',timeout=20000)
                                except Exception:pass
                                item['browser_images']=page.locator('.message.assistant .md img').evaluate_all('(images)=>images.map(i=>({src:i.getAttribute("src"),loaded:i.naturalWidth>0}))')
                                page.screenshot(path=str(output/'real-first-answer.png'),full_page=True)
                                first=False
                            else:
                                if session_id is None:
                                    r=admin.post('/api/v1/chat/sessions',json={});r.raise_for_status();session_id=r.json()['id']
                                with admin.stream('POST',f'/api/v1/chat/sessions/{session_id}/messages',json={'content':case['question']}) as response:
                                    response.raise_for_status()
                                    for line in response.iter_lines():
                                        consume_event(item,line)
                            stored=admin.get(f'/api/v1/chat/sessions/{session_id}/messages');stored.raise_for_status()
                            item['stored_answer']=stored.json()[-1]['content']
                            capture=output/'captures'/(key(case['question'])+'.json')
                            if capture.is_file():item['capture']=json.loads(capture.read_text(encoding='utf-8'))
                            prior.update(judge(case,item,prior))
                            admin_sessions.append(session_id)
                        except Exception as exc:item.update(errors=[type(exc).__name__],harness_error=str(exc)[:500],passed=False)
                        item['seconds']=round(time.monotonic()-started,1);save()
                        print(json.dumps({'group':group['id'],'seconds':item['seconds'],'passed':item.get('passed'),'errors':item.get('errors'),'availability':item.get('availability')},ensure_ascii=True),flush=True)
                    scenario['passed']=all(t.get('passed') for t in scenario['turns'])
                owned=admin_sessions[0]
                report['isolation']={'history_read_denied':normal.get(f'/api/v1/chat/sessions/{owned}/messages').status_code==404,
                    'history_write_denied':normal.post(f'/api/v1/chat/sessions/{owned}/messages',json={'content':'不可写入'}).status_code==404,
                    'history_delete_denied':normal.delete(f'/api/v1/chat/sessions/{owned}').status_code==404,
                    'management_denied':normal.get('/api/v1/knowledge/ops').status_code==403,
                    'usage_management_denied':normal.get('/api/v1/usage/admin').status_code==403,
                    'usage_personal_private':'global_day' not in normal.get('/api/v1/chat/quota').json(),
                    'session_list_private':normal.get('/api/v1/chat/sessions').json()==[]}
                # Restore a real live-guide session in the UI to verify final Markdown and preview requests.
                for scenario in report['groups']:
                    if not scenario['id'].startswith('live/'):continue
                    turn=scenario['turns'][0]
                    if '![' not in turn.get('answer',''):continue
                    page.reload(wait_until='domcontentloaded')
                    page.locator('.session-title').filter(has_text='验收 '+key(turn['question'])[:8]).click(timeout=15000)
                    page.locator('.message.assistant .md img').first.wait_for(timeout=10000)
                    try:page.wait_for_function('document.querySelector(".message.assistant .md img").naturalWidth>0',timeout=20000)
                    except Exception:pass
                    turn['browser_images']=page.locator('.message.assistant .md img').evaluate_all('(images)=>images.map(i=>({src:i.getAttribute("src"),loaded:i.naturalWidth>0}))')
                    page.screenshot(path=str(output/('real-'+scenario['id'].split('/')[1]+'.png')),full_page=True)
                page.get_by_role('button',name='知识库管理',exact=True).click()
                page.get_by_role('heading',name='模型用量与额度',exact=True).wait_for(timeout=15000)
                page.get_by_role('button',name='暂停全站新模型调用',exact=True).click()
                page.get_by_role('button',name='恢复全站新模型调用',exact=True).wait_for(timeout=10000)
                page.get_by_role('button',name='恢复全站新模型调用',exact=True).click()
                page.get_by_role('button',name='暂停全站新模型调用',exact=True).wait_for(timeout=10000)
                report['usage_admin_ui_pause_restore']=True
                page.screenshot(path=str(output/'usage-admin.png'),full_page=True)
                browser.close()
            report['diagnostics']=admin.get('/api/v1/knowledge/ops/retrievals').json()
            report['model_usage']=admin.get('/api/v1/usage/admin').json()
            report['model_calls']=[{'request_id':r['id'],**admin.get('/api/v1/usage/admin/requests/'+r['id']).json()}
                                   for r in report['model_usage'].get('requests',[])]
            report['real_titles']=real_titles
            report['passed']=all(g['passed'] for g in report['groups']) and all(report['isolation'].values())
            report['external_availability']={g['id']:g['turns'][0].get('external_content_available',False) for g in report['groups'] if g['id'].startswith('live/')}
            report['acceptance_complete']=report['passed'] and all(report['external_availability'].values())
            save();admin.close();normal.close()
        finally:
            server.terminate()
            try:server.wait(timeout=15)
            except subprocess.TimeoutExpired:server.kill();server.wait(timeout=5)
    return 0 if report['passed'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',help='Use configured real answer/intent models and public sources')
    parser.add_argument('--serve-fixture',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--port',type=int,default=0,help=argparse.SUPPRESS)
    parser.add_argument('--group',action='append')
    parser.add_argument('--real-titles',action='store_true',help='Meter real title generation; use single/local groups (live-guide UI lookup uses deterministic fixture titles)')
    parser.add_argument('--output',type=Path,default=ROOT/'backend/data/evaluations/2026-10-27-real-journeys')
    args=parser.parse_args()
    if args.serve_fixture:serve(args.port)
    elif not args.live:parser.error('Pass --live to opt in to real model and public-source requests')
    else:raise SystemExit(main(args.output.resolve(),args.group,args.real_titles))
