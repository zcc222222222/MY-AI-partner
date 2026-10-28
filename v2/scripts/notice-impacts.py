"""Inspect a saved announcement; optionally stage a source-pinned reviewed change manifest."""
import argparse
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from llmwiki import WikiStore
from llmwiki.tools import WIKI_PATH
from llmwiki.notice_impacts import candidates,build_page,fingerprint,linked_pages,active_pages
from llmwiki.updates import UpdateLock,atomic_json


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('source_id');ap.add_argument('--manifest');ap.add_argument('--stage',action='store_true')
    ap.add_argument('--draft-id');ap.add_argument('--db',default=str(WIKI_PATH));ap.add_argument('--output',required=True);args=ap.parse_args()
    if args.stage and not args.manifest:ap.error('--stage requires --manifest')
    store=WikiStore(args.db,create=False)
    with UpdateLock(store.path):
        source=store.get_source(args.source_id,game_version='documents-v1');doc=json.loads(source['text'])
        if doc.get('kind')!='official_notice':ap.error('source must be an official notice')
        report={'schema':'notice-impact-review-report-v1','source_id':source['source_id'],'source_revision':source['revision'],
                'source_sha256':fingerprint(source),'game_version':doc.get('game_version'),'candidates':candidates(source),'staged':False}
        if source['revision']>1:
            prior=store.get_source(args.source_id,game_version='documents-v1',revision=source['revision']-1)
            report['source_revision_comparison']={'previous_revision':prior['revision'],
                'readable_text_changed':json.loads(prior['text']).get('readable_text')!=doc.get('readable_text'),
                'note':'同一公告来源修订比较，不是前一游戏版本与本版本的机制差值。'}
        if args.manifest:
            manifest=json.loads(Path(args.manifest).read_text(encoding='utf-8'))
            if manifest['source_id']!=source['source_id']:ap.error('manifest belongs to another source')
            current=store.get_page(manifest['page_id'],game_version='documents-v1',allow_stale=True)['page'];target=current
            if args.draft_id:
                with store.connection() as conn:row=conn.execute('SELECT payload FROM drafts WHERE draft_id=?',(args.draft_id,)).fetchone()
                if not row:ap.error('draft not found')
                target=json.loads(row[0])
                if target['page_id']!=manifest['page_id']:ap.error('draft belongs to another page')
            page=build_page(source,target,manifest);pages=active_pages(store)
            report['reviewed_entries']=[{**e,'related_pages':linked_pages(e,pages)} for e in page['notice_impacts']['entries']]
            report['unchanged']=current.get('notice_impacts')==page['notice_impacts'] and current['sources']==page['sources']
            if args.stage and not report['unchanged']:
                backup=store.path.parent/'backups'/('before-notice-impacts-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.db');backup.parent.mkdir(parents=True,exist_ok=True)
                with closing(sqlite3.connect(store.path)) as original,closing(sqlite3.connect(backup)) as dest:original.backup(dest)
                report.update(backup=str(backup),draft=store.stage_page(page),staged=True)
        atomic_json(args.output,report)
    print(json.dumps({k:v for k,v in report.items() if k not in ('candidates','reviewed_entries')},ensure_ascii=False))
    return 0 if not report['staged'] or report['draft']['valid'] else 1


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');raise SystemExit(main())
