"""Stage pinned gap follow-ups; explicit review and publish remain separate."""
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
from llmwiki.guide_followup import build_followup_page
from llmwiki.updates import UpdateLock


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('manifest',nargs='+');ap.add_argument('--db',default=str(WIKI_PATH));ap.add_argument('--stage',action='store_true');ap.add_argument('--output',required=True);ap.add_argument('--draft-id',help='Apply to a reviewed repair draft after the original source changes');args=ap.parse_args()
    store=WikiStore(args.db,create=False);reports=[]
    with UpdateLock(store.path):
        manifests=[json.loads(Path(p).read_text(encoding='utf-8')) for p in args.manifest]
        if args.draft_id and len(manifests)!=1:ap.error('--draft-id只能对应一篇')
        if len({m['page_id'] for m in manifests})!=len(manifests):ap.error('重复目标页面')
        for manifest in manifests:
            source=store.get_source(manifest['source_id'],game_version='documents-v1');current=store.get_page(manifest['page_id'],game_version='documents-v1',allow_stale=True)['page'];target=current
            if args.draft_id:
                with store.connection() as conn:row=conn.execute('SELECT payload FROM drafts WHERE draft_id=?',(args.draft_id,)).fetchone()
                if not row:ap.error('指定草稿不存在')
                target=json.loads(row[0])
                if target['page_id']!=manifest['page_id']:ap.error('草稿不属于目标页面')
            page=build_followup_page(source,target,manifest)
            reports.append({'page':page,'unchanged':page.get('guide_followup')==current.get('guide_followup') and page.get('pages')==current.get('pages'),'staged':False})
        if args.stage and any(not r['unchanged'] for r in reports):
            backup=store.path.parent/'backups'/('before-guide-followup-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.db')
            backup.parent.mkdir(parents=True,exist_ok=True)
            with closing(sqlite3.connect(store.path)) as original,closing(sqlite3.connect(backup)) as dest:original.backup(dest)
            for report in reports:
                if report['unchanged']:continue
                report.update(backup=str(backup),draft=store.stage_page(report['page']),staged=True)
    Path(args.output).write_text(json.dumps({'reports':reports},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps([{k:v for k,v in r.items() if k!='page'} for r in reports],ensure_ascii=False))
    return 0 if all(not r['staged'] or r['draft']['valid'] for r in reports) else 1


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');raise SystemExit(main())
