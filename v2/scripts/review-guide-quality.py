"""Validate or stage a source-pinned quality review; review/publish remain explicit."""
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
from llmwiki.guide_quality import build_quality_page
from llmwiki.updates import UpdateLock


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('manifest');ap.add_argument('--db',default=str(WIKI_PATH))
    ap.add_argument('--draft-id',help='Apply to a new compilation draft after a source update, keeping its new evidence and metadata')
    ap.add_argument('--stage',action='store_true');ap.add_argument('--output',required=True);args=ap.parse_args()
    manifest=json.loads(Path(args.manifest).read_text(encoding='utf-8'));store=WikiStore(args.db,create=False)
    with UpdateLock(store.path):
        source=store.get_source(manifest['source_id'],game_version='documents-v1')
        current=store.get_page(manifest['page_id'],game_version='documents-v1',allow_stale=True)
        target=current['page']
        if args.draft_id:
            with store.connection() as c:row=c.execute('SELECT payload FROM drafts WHERE draft_id=?',(args.draft_id,)).fetchone()
            if not row:ap.error('指定草稿不存在')
            target=json.loads(row[0])
            if target['page_id']!=manifest['page_id']:ap.error('草稿不属于目标页面')
        page=build_quality_page(source,target,manifest)
        report={'page':page,'unchanged':page.get('guide_quality')==current['page'].get('guide_quality'),'staged':False}
        if args.stage and not report['unchanged']:
            backup=store.path.parent/'backups'/('before-guide-quality-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.db')
            backup.parent.mkdir(parents=True,exist_ok=True)
            with closing(sqlite3.connect(store.path)) as original,closing(sqlite3.connect(backup)) as dest:original.backup(dest)
            report.update(backup=str(backup),draft=store.stage_page(page),staged=True)
    out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='page'},ensure_ascii=False))
    return 0 if not report['staged'] or report['draft']['valid'] else 1


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');raise SystemExit(main())
