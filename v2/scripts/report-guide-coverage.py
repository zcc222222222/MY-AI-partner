"""Offline roster-to-published-guide coverage, including explicit source gaps."""
import argparse
from collections import Counter
from importlib import import_module
import json
from pathlib import Path
import sqlite3
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from llmwiki import WikiStore
from llmwiki.tools import WIKI_PATH,KNOWLEDGE_PATH
from llmwiki.guide_curation import fingerprint
from llmwiki.documents import validate_page


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--output',required=True);args=ap.parse_args()
    store=WikiStore(WIKI_PATH,create=False);rows=[];errors=[]
    for item in import_module('inventory-guide-curation').inventory(store)['pages']:
        source=store.get_source(item['source_id'],game_version='documents-v1')
        current=store.get_page(item['page_id'],game_version='documents-v1');page=current['page']
        try:validate_page(source,page)
        except Exception as exc:errors.append({'page_id':item['page_id'],'error':str(exc)})
        data=page.get('guide_curation',{});review=data.get('coverage_review',{})
        held=page.get('guide_quality',{}).get('status')=='pending_verification'
        status='quality_hold' if held else review.get('status') or ('reviewed_text' if data.get('rotations') else 'unreviewed')
        if status=='unreviewed':errors.append({'page_id':item['page_id'],'error':'unclassified_source'})
        paths=[]
        for f in data.get('rotations',[]):
            path=f['spec'].get('source_path',data['source_path'])
            if path not in paths:paths.append(path)
        rows.append({**{k:item[k] for k in ('page_id','source_id','title','source_revision','page_revision')},
            'source_sha256':fingerprint(source),'status':status,'entity_ids':page['document_metadata']['entity_ids'],
            'platform_date':page['document_metadata']['source_date'],'applicable_game_version':page['document_metadata']['applicable_game_version'],
            'segments':sum(len(f['segments']) for f in data.get('rotations',[])),'rotation_source_paths':paths,
            'note':review.get('note') or ('待核对，仅返回来源信息。' if held else '已有来源内流程整理；不表示整篇事实、所有链数或游戏版本均已验证。'),
            'phases':[{'phase':s['phase'],'scope':f['spec'].get('coverage_scope','source_segment'),'chain_scope':f['spec']['chain_scope'],
                       'cycle_reference':s.get('cycle_reference')} for f in data.get('rotations',[]) for s in f['segments']]})
    with sqlite3.connect(KNOWLEDGE_PATH.resolve().as_uri()+'?mode=ro',uri=True) as conn:
        roster=[{'id':str(i),'name':n} for i,n in conn.execute('SELECT id,name FROM characters ORDER BY id')]
    for role in roster:
        role['sources']=[r['source_id'] for r in rows if 'character:'+role['id'] in r['entity_ids']]
        role['coverage_mode']='direct'
        if not role['sources'] and role['name'] in ('漂泊者·衍射','漂泊者·湮灭','漂泊者·气动','漂泊者·导电'):
            peers=[p['id'] for p in roster if p['name']==role['name'] and p['id']!=role['id']]
            role['sources']=[r['source_id'] for r in rows if any('character:'+pid in r['entity_ids'] for pid in peers)]
            if role['sources']:role['coverage_mode']='shared_element_form'
        role['statuses']=list(dict.fromkeys(r['status'] for r in rows if r['source_id'] in role['sources']))
        if not role['sources']:errors.append({'role':role,'error':'missing_guide'})
        from llmwiki.tools import search_documents
        hit=search_documents('攻略',category='guide',entity_name=role['id'])
        returned={p['document_metadata']['source_id'] for p in hit.get('results',[])}
        role['actual_retrieval_passed']=hit.get('status')=='found' and bool(returned) and returned<=set(role['sources'])
        if not role['actual_retrieval_passed']:errors.append({'role_id':role['id'],'error':'actual_retrieval_mismatch','returned':sorted(returned)})
    report={'schema':'all-role-guide-coverage-v1','passed':not errors,'scope':'current local roster and latest saved guide catalog, not live website availability or gameplay validation',
        'summary':{'character_records':len(roster),'guide_sources':len(rows),'source_statuses':dict(Counter(r['status'] for r in rows)),
                   'sources_with_rotations':sum(r['segments']>0 for r in rows),'rotation_segments':sum(r['segments'] for r in rows)},
        'roles':roster,'sources':rows,'errors':errors}
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'summary':report['summary'],'errors':errors},ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');raise SystemExit(main())
