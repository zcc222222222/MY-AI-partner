"""Plan or stage pinned source-local guide annotations. Review and publish separately."""
import argparse
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from llmwiki import WikiStore
from llmwiki.tools import WIKI_PATH,KNOWLEDGE_PATH
from llmwiki.guide_curation import build_page
from llmwiki.payload import compact
from llmwiki.updates import UpdateLock


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('manifest',nargs='+')
    ap.add_argument('--db',default=str(WIKI_PATH))
    ap.add_argument('--knowledge-db',default=str(KNOWLEDGE_PATH))
    ap.add_argument('--stage',action='store_true')
    ap.add_argument('--output',required=True)
    args=ap.parse_args()
    manifests=[json.loads(Path(p).read_text(encoding='utf-8')) for p in args.manifest]
    if len({m['page_id'] for m in manifests})!=len(manifests):ap.error('同一批不能重复标注同一页面')
    store=WikiStore(args.db,create=False)
    with UpdateLock(store.path):
        reports=[]
        # Validate every source pin before creating any draft in the batch.
        for manifest in manifests:
            source=store.get_source(manifest['source_id'],game_version='documents-v1')
            current=store.get_page(manifest['page_id'],game_version='documents-v1')
            page=build_page(source,current['page'],manifest,Path(args.knowledge_db))
            normalized=compact(page);previous=compact(current['page'])
            unchanged=all(normalized.get(k)==previous.get(k) for k in ('guide_curation','body','claims','citations'))
            reports.append({'page':page,'base_revision':current['revision'],'staged':False,'unchanged':unchanged})
        if args.stage and any(not r['unchanged'] for r in reports):
            backup=store.path.parent/'backups'/('before-guide-curation-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.db')
            backup.parent.mkdir(parents=True,exist_ok=True)
            with closing(sqlite3.connect(store.path)) as original,closing(sqlite3.connect(backup)) as dest:original.backup(dest)
            for report in reports:
                if report['unchanged']:continue
                report['backup']=str(backup)
                report['draft']=store.stage_page(report['page'])
                report['staged']=True
        report=reports[0] if len(reports)==1 else {'reports':reports}
    Path(args.output).parent.mkdir(parents=True,exist_ok=True)
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps([{k:v for k,v in r.items() if k!='page'} for r in reports],ensure_ascii=False))
    return 0 if all(not r['staged'] or r['draft']['valid'] for r in reports) else 1


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
