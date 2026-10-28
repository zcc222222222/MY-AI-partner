"""Pinned player question regression. Default: evidence only. --live: actual chat model."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from llmwiki import WikiStore
from llmwiki.tools import WIKI_PATH,search_documents
from app.agents import orchestrator,tools
from app.agents.roleplay import build_system_prompt
from app.services import retrieval_diagnostics as diagnostics
from app.services.chat_service import _source_suffix,_image_suffix,_with_budget_fallback,_grounded_text


def evidence(case,store):
    errors=[]
    if case.get('source'):
        source=store.get_source(case['source'],game_version='documents-v1')
        if not source or source['revision']!=case['revision']:
            return {'passed':False,'errors':['gold_stale_review_source_revision']}
    arguments={'query':case['query'],'category':'guide','entity_name':case['entity'],
               **{k:case[k] for k in ('game_version','author','updated_on') if k in case}}
    record=diagnostics.begin('search_documents',arguments,origin='regression')
    result=diagnostics.run('search_documents',arguments,lambda:search_documents(**arguments),record=record)
    if record.get('reason') != ('no_match' if case.get('empty') else 'success'):
        errors.append('unexpected_diagnostic_reason:'+str(record.get('reason')))
    text='\n'.join(m['text'] for p in result.get('results',[]) for m in p['matches'])
    if case.get('empty'):
        if result.get('status')!='no_reviewed_match':errors.append('expected_no_reviewed_match')
    else:
        if result.get('status')!='found':errors.append('not_found')
        for value in case.get('need',[]):
            if value not in text:errors.append('missing:'+value)
        for value in case.get('not_need',[]):
            if value in text:errors.append('unexpected:'+value)
        for p in result.get('results',[]):
            if any(s['source_id']!=case['source'] or s['revision']!=case['revision'] for s in p['source_refs']):errors.append('source_mismatch')
    if len(json.dumps(result,ensure_ascii=False))>8000:errors.append('payload_budget')
    return {'passed':not errors,'errors':errors,'result':result,'diagnostics':diagnostics.public(record),
            **({'source_sha256':hashlib.sha256(source['text'].encode()).hexdigest()} if case.get('source') else {})}


def live(case,system):
    item={'answer':'','calls':[],'traces':[],'intent':None,'errors':[]}
    sources=[]
    original=orchestrator.execute_tool
    def execute(name,args):
        result=original(name,args)
        item['calls'].append({'name':name,'arguments':json.loads(args),'result':json.loads(result)})
        return result
    try:
        with patch.object(orchestrator,'execute_tool',side_effect=execute):
            messages=[{'role':'system','content':system},*case.get('history',[]),{'role':'user','content':case['question']}]
            for event in _with_budget_fallback(orchestrator.run_agent(messages,tools=tools.TOOLS,intent_routing=True)):
                if event['type']=='delta':item['answer']+=event['content']
                elif event['type']=='replace':item['answer']=event['content'];item['errors'].append('generation_fallback')
                elif event['type']=='intent_trace':item['intent']=event['trace']
                elif event['type']=='knowledge_trace':item['traces'].append(event['trace']);sources.extend(event['sources'])
        if item['traces']:
            item['raw_answer']=item['answer']
            item['answer']=_grounded_text(item['answer'])
        body=item['answer']
        for options in case.get('answer_need',[]):
            if not any(value in body for value in options):item['errors'].append('missing_answer_term:'+str(options))
        for value in case.get('answer_not_need',[]):
            if value in body:item['errors'].append('unsupported_answer_term:'+value)
        item['answer']+=_source_suffix(sources,item['answer'])
        if not item['intent'] or item['intent']['image_mode']!='none':item['answer']+=_image_suffix(sources,item['answer'])
        if case.get('images')=='none' and ('![' in item['answer'] or item['intent']['image_mode']!='none'):item['errors'].append('declined_images')
        delivered=[s for call in item['calls'] if call['name']=='search_documents' for p in call['result'].get('results',[]) for s in p['source_refs']]
        if not case.get('empty'):
            if not delivered:item['errors'].append('no_document_evidence')
            if any(s['source_id']!=case['source'] or s['revision']!=case['revision'] for s in delivered):item['errors'].append('wrong_live_source')
        elif not item['intent'] or item['intent']['game_version']!=case.get('game_version') or delivered:
            item['errors'].append('version_scope_not_preserved')
        if not body.strip():item['errors'].append('empty_answer')
    except Exception as exc:
        item['errors'].append(type(exc).__name__)
    item['passed']=not item['errors']
    return item


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--live',action='store_true')
    ap.add_argument('--output',required=True)
    ap.add_argument('--case',action='append')
    args=ap.parse_args()
    suite=json.loads((ROOT/'tests/player_questions.json').read_text(encoding='utf-8'))
    output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
    diagnostics.ROOT=output.parent/'retrieval-diagnostics'
    report={'schema':'player-answer-acceptance-v1','inspiration':suite['inspiration'],'cases':[],
            'limits':'字面检查与来源校验不等于逐句语义审核；回答仍需对照原文复核。'}
    system=build_system_prompt(SimpleNamespace(**json.loads((ROOT/'backend/characters/amis.json').read_text(encoding='utf-8'))))
    store=WikiStore(WIKI_PATH,create=False)
    for case in suite['cases']:
        if args.case and case['id'] not in args.case:continue
        item={'id':case['id'],'question':case['question'],'evidence':evidence(case,store)}
        if args.live and case.get('live'):item['live']=live(case,system)
        report['cases'].append(item)
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'id':case['id'],'evidence_passed':item['evidence']['passed'],'live_passed':item.get('live',{}).get('passed'),
                          'errors':item['evidence']['errors']+item.get('live',{}).get('errors',[])},ensure_ascii=False),flush=True)
    report['passed']=all(c['evidence']['passed'] and c.get('live',{}).get('passed',True) for c in report['cases']) and bool(report['cases'])
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if report['passed'] else 1


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
