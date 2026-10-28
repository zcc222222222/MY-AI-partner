"""Offline acceptance of a completed latest-only catalog sync and actual retrieval."""
import argparse
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from knowledge.config import DATA_DIR
from llmwiki import WikiStore
from llmwiki.updates import plan_documents, source_snapshot, atomic_json
from llmwiki import tools


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--run-id',required=True)
    ap.add_argument('--db',default=str(Path(DATA_DIR)/'wiki/wiki.db'))
    ap.add_argument('--knowledge-db',default=str(Path(DATA_DIR)/'knowledge.db'))
    ap.add_argument('--output',required=True)
    args=ap.parse_args()
    import re
    if not re.fullmatch('[0-9a-f]{32}',args.run_id): ap.error('无效run_id')
    store=WikiStore(args.db,create=False)
    master=json.loads((store.path.parent/'latest-guides'/args.run_id/'report.json').read_text(encoding='utf-8'))
    sources=source_snapshot(store)
    plan=plan_documents(store)
    output={'run_id':args.run_id,'checks':[],'planning_errors':plan['errors'],'held_sources':[]}
    def record(name,ok,**details):
        output['checks'].append({'name':name,'passed':bool(ok),**details})
    record('master_completed',master['state']=='completed' and not master['errors'])
    with patch.object(tools,'WIKI_PATH',store.path),patch.object(tools,'KNOWLEDGE_PATH',Path(args.knowledge_db)):
        for item in master['roles']:
            role=item['role_id']
            selected=item.get('selected_source')
            active=[s for s in sources if s.startswith('mcguide/'+role+'/')]
            record(role+':one_active_source',item['status']=='completed' and active==[selected],active=active)
            jobs=[j for j in plan['pages'] if j['source_id']==selected]
            record(role+':reviewed_chapters',bool(jobs) and all(j['status']=='unchanged' for j in jobs))
            selections=item.get('selections',[])
            chosen=selections[-1] if selections else {}
            candidates=chosen.get('candidates',[])
            record(role+':latest_selection',bool(candidates) and chosen.get('selected_ids')==[candidates[0]['id']]
                   and selected=='mcguide/'+role+'/'+str(candidates[0]['id']))
            hit=tools.search_documents('攻略',category='guide',entity_name=role,limit=5)
            pages=[p['page_id'] for p in hit.get('results',[])]
            record(role+':actual_retrieval',hit.get('status')=='found' and bool(pages)
                   and all(p.startswith('document/'+str(selected)+'/part-') for p in pages),pages=pages)
            held=[p for p in hit.get('results',[]) if p.get('quality_review',{}).get('status')=='pending_verification']
            if held:
                output['held_sources'].append(selected)
                record(role+':quality_hold_metadata_only',all(p.get('excerpt_topics')==['quality_hold'] and not p.get('images')
                       and all(m['heading']=='来源与日期' for m in p['matches']) for p in held))
    output['passed']=not output['planning_errors'] and all(c['passed'] for c in output['checks'])
    output['check_count']=len(output['checks'])
    atomic_json(Path(args.output),output)
    print(json.dumps({'passed':output['passed'],'check_count':output['check_count'],
                      'failures':[c for c in output['checks'] if not c['passed']]},ensure_ascii=False))
    return 0 if output['passed'] else 1


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
