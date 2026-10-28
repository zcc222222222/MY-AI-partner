"""Real successive turns through chat_service in an isolated temporary application DB."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from sqlalchemy import create_engine,event,select
from sqlalchemy.orm import sessionmaker
from app import models
from app.agents import orchestrator
from app.migrations import migrate
from app.services import chat_service
from llmwiki import WikiStore
from llmwiki.tools import WIKI_PATH


def check_turn(case,item):
    errors=[];plan=item.get('intent') or {}
    docs=[c for c in item.get('calls',[]) if c['name']=='search_documents']
    recovered=(plan.get('status')=='fallback' and not plan.get('ambiguous') and bool(docs)
               and all(c['result'].get('trace',{}).get('entity')==case['entity'] for c in docs))
    if plan.get('ambiguous') or (plan.get('status')!='parsed' and not recovered):errors.append('intent_not_parsed')
    if set(plan.get('entity_keys',[]))!={case['entity']} and not recovered:errors.append('entity_scope')
    if plan.get('game_version')!=case['game_version']:errors.append('version_scope')
    if plan.get('image_mode')!=case['image_mode']:errors.append('image_preference')
    if not item.get('complete'):errors.append('generation_incomplete')
    if item.get('answer')!=item.get('stored_answer'):errors.append('stream_storage_mismatch')
    if not item.get('answer','').strip():errors.append('empty_answer')
    if case['image_mode']=='none' and '![' in item.get('answer',''):errors.append('declined_images')
    reusable=case.get('allow_source_reuse') and case.get('required_source') in item.get('prior_sources',[])
    if not docs and not reusable:errors.append('no_document_call')
    sources={s['source_id'] for c in docs for p in c['result'].get('results',[]) for s in p.get('source_refs',[])}
    if case.get('empty'):
        if sources or any(c['result'].get('status')!='no_reviewed_match' for c in docs):errors.append('expected_no_match')
    else:
        if case['required_source'] not in sources and not reusable:errors.append('missing_required_source')
        if sources-set(case['allowed_sources']):errors.append('wrong_source')
    chain_checked=False
    for call in docs:
        args=call['arguments']
        if args.get('game_version')!=case['game_version']:errors.append('tool_version_scope')
        if case.get('no_date_filter') and (args.get('updated_on') or args.get('as_of')):errors.append('invented_date_filter')
        if len(json.dumps(call['result'],ensure_ascii=False))>8000:errors.append('tool_budget')
        if case.get('uncovered_chain') and re.search(r'零链|0链',args.get('query','')):
            chain_checked=True
            if any(p.get('guide_context',{}).get('rotation_segments') for p in call['result'].get('results',[])):
                errors.append('unverified_chain_flow_delivered')
    if case.get('uncovered_chain') and not chain_checked:errors.append('no_explicit_chain_check')
    for options in case.get('answer_need',[]):
        if not any(value in item.get('answer','') for value in options):errors.append('answer_term:'+str(options))
    return sorted(set(errors))


def recheck_report(previous,suite):
    result=deepcopy(previous)
    scenarios={s['id']:s for s in suite['scenarios']}
    if {s['id'] for s in result['scenarios']}!=set(scenarios):raise ValueError('scenario set differs from suite')
    for scenario in result['scenarios']:
        cases=scenarios[scenario['id']]['turns']
        if len(cases)!=len(scenario['turns']):raise ValueError('turn count differs from suite')
        # Derive prior evidence again; never trust a stored list as provenance.
        prior=set()
        for case,item in zip(cases,scenario['turns']):
            if item['question']!=case['question']:raise ValueError('question differs from suite')
            item['prior_sources']=sorted(prior);item['previous_errors']=item['errors']
            item['errors']=check_turn(case,item);item['passed']=not item['errors']
            prior.update(ref['source_id'] for call in item['calls'] if call['name']=='search_documents'
                         for page in call['result'].get('results',[]) for ref in page.get('source_refs',[]))
        scenario['passed']=all(t['passed'] for t in scenario['turns'])
    result['passed']=all(s['passed'] for s in result['scenarios'])
    return result


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--live',action='store_true');ap.add_argument('--scenario',action='append')
    ap.add_argument('--recheck',help='Re-evaluate a saved complete transcript without calling a model; preserves previous errors')
    ap.add_argument('--output',required=True);args=ap.parse_args()
    if bool(args.live)==bool(args.recheck):ap.error('choose either --live or --recheck')
    if args.recheck and args.scenario:ap.error('--recheck requires a full suite transcript')
    suite=json.loads((ROOT/'tests/conversation_questions.json').read_text(encoding='utf-8'))
    selected=[s for s in suite['scenarios'] if not args.scenario or s['id'] in args.scenario]
    if not selected or (args.scenario and set(args.scenario)-{s['id'] for s in selected}):ap.error('unknown or empty scenario selection')
    out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True)
    report={'schema':'conversation-acceptance-v1','passed':False,'scenarios':[], 'sources':{},
            'limits':'真实回复逐轮落库并作为下一轮历史；自动检查仅验证交付、来源、条件关键词，仍需人工对照。使用临时账号与会话，不修改正式用户数据。'}
    store=WikiStore(WIKI_PATH,create=False)
    for sid,revision in suite['sources'].items():
        source=store.get_source(sid,game_version='documents-v1')
        if not source or source['revision']!=revision:ap.error('gold_stale_review_source_revision: '+sid)
        report['sources'][sid]={'revision':revision,'sha256':hashlib.sha256(source['text'].encode()).hexdigest()}
    def save():out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    if args.recheck:
        previous=json.loads(Path(args.recheck).read_text(encoding='utf-8'))
        if previous.get('sources')!=report['sources']:ap.error('saved source fingerprints differ from current pins')
        report=recheck_report(previous,suite);report['rechecked_from']=str(Path(args.recheck).resolve())
        save();print(json.dumps({'passed':report['passed'],'scenarios':len(report['scenarios'])}));return 0 if report['passed'] else 1
    with tempfile.TemporaryDirectory() as folder:
        engine=create_engine('sqlite:///'+(Path(folder)/'app.db').as_posix())
        event.listen(engine,'connect',lambda conn,_:conn.execute('PRAGMA foreign_keys=ON'))
        try:
            migrate(engine)
            with sessionmaker(engine)() as db:
                card=json.loads((ROOT/'backend/characters/amis.json').read_text(encoding='utf-8'))
                db.add(models.User(id=1,username='conversation-probe',display_name='验收',password_hash='unused'))
                db.add(models.Character(id=1,slug='probe',name=card['name'],display_name=card['display_name'],personality=card['personality'],system_prompt=card['system_prompt']))
                db.commit()
                for scenario in selected:
                    sid=scenario['id'];db.add(models.ChatSession(id=sid,owner_id=1,character_id=1));db.commit()
                    entry={'id':sid,'turns':[]};report['scenarios'].append(entry)
                    for case in scenario['turns']:
                        prior_sources=sorted({ref['source_id'] for t in entry['turns'] for c in t['calls'] if c['name']=='search_documents'
                                              for p in c['result'].get('results',[]) for ref in p.get('source_refs',[])})
                        item={'question':case['question'],'answer':'','calls':[],'intent':None,'traces':[],'usage':[],'prior_sources':prior_sources}
                        original_execute=orchestrator.execute_tool;original_agent=chat_service.run_agent
                        def execute(name,arguments):
                            result=original_execute(name,arguments)
                            item['calls'].append({'name':name,'arguments':json.loads(arguments),'result':json.loads(result)})
                            return result
                        def agent(messages,**kwargs):
                            item['input_history_sha256']=hashlib.sha256(json.dumps(messages,ensure_ascii=False).encode()).hexdigest()
                            item['history_messages']=len(messages)-1
                            for e in original_agent(messages,**kwargs):
                                if e['type']=='intent_trace':item['intent']=e['trace']
                                elif e['type']=='knowledge_trace':item['traces'].append(e['trace'])
                                elif e['type']=='usage':item['usage'].append(e['usage'])
                                yield e
                        def authorize():
                            if db.scalar(select(models.ChatSession.id).where(models.ChatSession.id==sid,models.ChatSession.owner_id==1)) is None:
                                raise RuntimeError('temporary session access lost')
                        try:
                            # Title generation is presentation; all answer, intent and knowledge calls remain real.
                            with patch.object(chat_service,'run_agent',side_effect=agent),patch.object(orchestrator,'execute_tool',side_effect=execute),patch.object(chat_service.session_service,'generate_title',return_value='连续对话验收'):
                                for payload in chat_service.stream_reply(db,db.get(models.ChatSession,sid),db.get(models.Character,1),case['question'],authorize=authorize):
                                    e=json.loads(payload)
                                    if e['type']=='delta':item['answer']+=e['content']
                                    elif e['type']=='replace':item['answer']=e['content']
                                    elif e['type']=='done':item['complete']=e.get('status')=='complete';item['message_id']=e['message_id']
                            stored=db.get(models.Message,item.get('message_id')) if item.get('message_id') else None
                            item['stored_answer']=stored.content if stored else None
                            item['errors']=check_turn(case,item)
                        except Exception as exc:item['errors']=[type(exc).__name__]
                        item['passed']=not item['errors'];entry['turns'].append(item);save()
                        print(json.dumps({'scenario':sid,'turn':len(entry['turns']),'passed':item['passed'],'errors':item['errors']},ensure_ascii=False),flush=True)
                    entry['passed']=all(t['passed'] for t in entry['turns']);save()
        finally:engine.dispose()
    report['passed']=all(s['passed'] for s in report['scenarios']);save()
    return 0 if report['passed'] else 1


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');raise SystemExit(main())
