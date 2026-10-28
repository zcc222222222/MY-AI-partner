"""Verify every pinned curation against published sources and the bounded chat tool."""
import argparse
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from llmwiki import WikiStore
from llmwiki.guide_curation import build_page
from llmwiki.payload import compact
from llmwiki.tools import WIKI_PATH,KNOWLEDGE_PATH,search_documents


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--output',required=True);args=ap.parse_args()
    store=WikiStore(WIKI_PATH,create=False);checks=[]
    for path in sorted((ROOT/'backend/llmwiki/curated_guides').glob('*.json')):
        manifest=json.loads(path.read_text(encoding='utf-8'));sid=manifest['source_id'];pid=manifest['page_id']
        try:
            source=store.get_source(sid,game_version='documents-v1');current=store.get_page(pid,game_version='documents-v1')['page']
            rebuilt=compact(build_page(source,current,manifest,KNOWLEDGE_PATH));published=compact(current)
            assert all(rebuilt.get(k)==published.get(k) for k in ('guide_curation','claims','body','citations')),'published_manifest_mismatch'
            checks.append({'source_id':sid,'check':'published_manifest','passed':True})
            data=current['guide_curation'];expected={s['claim_id'] for f in data['rotations'] for s in f['segments']}
            queries=[('完整手法',expected)]
            for flow in data['rotations']:
                for segment in flow['segments']:
                    phase=segment['phase'];cue=phase
                    if phase=='配队手法':cue='启动轴'
                    if phase=='原文流程':cue='完整手法'
                    block=next((b for b in flow['spec'].get('blocks',[]) if b['phase']==phase),{})
                    if block.get('selectors'):cue=block['selectors'][0]+' 手法'
                    if flow['spec']['mode'] in ('震谐','聚爆'):cue=flow['spec']['mode']+' '+cue
                    if flow['spec'].get('team_variant'):cue=flow['spec']['team_variant']['selectors'][0]+' '+cue
                    if flow['spec'].get('playstyle'):cue=flow['spec']['playstyle']['selectors'][0]+' 手法 '+cue
                    wanted={segment['claim_id']}
                    if phase==flow['spec'].get('team_variant',{}).get('label') and flow['spec'].get('team_variant',{}).get('members'):
                        wanted={s['claim_id'] for s in flow['segments']}
                    queries.append((cue,wanted))
            for query,wanted in queries:
                role=str(json.loads(source['text']).get('role_id') or sid.split('/')[1])
                version=current.get('document_metadata',{}).get('applicable_game_version') if current.get('document_metadata',{}).get('source_role')=='historical_supplement' else None
                result=search_documents(query,category='guide',entity_name=role,game_version=version)
                pages=[p for p in result.get('results',[]) if p['page_id']==pid]
                actual={s['claim_id'] for p in pages for s in p.get('guide_context',{}).get('rotation_segments',[])}
                assert actual==wanted,('phase_coverage',query,sorted(actual),sorted(wanted))
                assert len(json.dumps(result,ensure_ascii=False))<=8000,'tool_budget'
                delivered='\n'.join(m['text'] for p in pages for m in p['matches'])
                for flow in data['rotations']:
                    for segment in flow['segments']:
                        if segment['claim_id'] in wanted:
                            assert segment['raw'] in delivered,'raw_segment_changed'
                            for ref in segment.get('dependencies',[]):assert ref['raw'] in delivered,'dependency_excerpt_missing'
                checks.append({'source_id':sid,'check':'retrieval','query':query,'passed':True,'claims':sorted(actual)})
        except Exception as exc:
            checks.append({'source_id':sid,'check':'failed','passed':False,'error':str(exc)})
    report={'schema':'all-curated-guides-check-v1','passed':bool(checks) and all(c['passed'] for c in checks),
            'sources':len({c['source_id'] for c in checks}),'check_count':len(checks),'checks':checks}
    out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='checks'},ensure_ascii=False))
    for c in checks:
        if not c['passed']:print(json.dumps(c,ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');raise SystemExit(main())
