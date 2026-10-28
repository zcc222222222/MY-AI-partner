"""Inventory current reviewed guide pages using saved source text only; no crawling."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from llmwiki import WikiStore
from llmwiki.guide_curation import source_text, fingerprint
from llmwiki.tools import WIKI_PATH


def inventory(store):
    with store.connection() as conn:
        ids=[r[0] for r in conn.execute("""SELECT p.page_id FROM pages p JOIN page_revisions r
             ON r.page_id=p.page_id AND r.game_version=p.game_version AND r.locale=p.locale AND r.revision=p.active_revision
             WHERE p.game_version='documents-v1' AND p.page_id LIKE 'document/mcguide/%'
             AND EXISTS (SELECT 1 FROM drafts d JOIN reviews v ON v.draft_id=d.draft_id AND v.fingerprint=d.fingerprint
                WHERE d.page_id=p.page_id AND d.game_version=p.game_version AND d.locale=p.locale AND d.fingerprint=r.fingerprint
                AND d.state='published' AND v.review_id=(SELECT MAX(review_id) FROM reviews WHERE draft_id=d.draft_id)
                AND v.decision='approved') ORDER BY p.page_id""")]
    rows=[]
    for pid in ids:
        current=store.get_page(pid,game_version='documents-v1',allow_stale=True)
        if not current or current.get('dirty'):continue
        page=current['page'];sid=page['document_metadata']['source_id']
        source=store.get_source(sid,game_version='documents-v1')
        doc=json.loads(source['text']);fields=[]
        for index,block in enumerate(doc.get('content',{}).get('baseTexts',[])):
            if block.get('language')!='zh-Hans' or not isinstance(block.get('introductionDetail'),str):continue
            path=['content','baseTexts',index,'introductionDetail']
            lines=source_text(source,path).splitlines()
            fields.append({'source_path':path,'arrow_lines':[{'line':i+1,'text':line} for i,line in enumerate(lines) if '→' in line]})
        curated=page.get('guide_curation',{})
        rows.append({'page_id':pid,'source_id':sid,'title':doc['title'],'source_revision':source['revision'],
                     'source_sha256':fingerprint(source),'page_revision':current['revision'],
                     'curated_segments':sum(len(f['segments']) for f in curated.get('rotations',[])),
                     'curated_terms':len(curated.get('terms',[])),'fields':fields})
    return {'schema':'guide-curation-inventory-v1','note':'箭头仅是待核对线索，不代表可执行输出轴、完整流程或事实正确；范围为当前可读攻略页面的中文基础说明字段。',
            'pages':rows,'summary':{'sources':len({r['source_id'] for r in rows}),'pages':len(rows),
            'pages_with_arrow_candidates':sum(any(f['arrow_lines'] for f in r['fields']) for r in rows),
            'curated_pages':sum(r['curated_segments']>0 or r['curated_terms']>0 for r in rows),
            'curated_segments':sum(r['curated_segments'] for r in rows)}}


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--db',default=str(WIKI_PATH));ap.add_argument('--output',required=True);args=ap.parse_args()
    result=inventory(WikiStore(args.db,create=False));out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result['summary'],ensure_ascii=False))


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');main()
