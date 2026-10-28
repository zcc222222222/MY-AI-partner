"""Sync the entire verified guide catalog, selecting one newest Chinese guide per role."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from knowledge.config import DATA_DIR
from llmwiki import WikiStore
from llmwiki.guide_coverage import load_catalog, refresh_catalog
from llmwiki.collector import Collector
from llmwiki.latest_guides import full_plan, sync_latest


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--live',action='store_true')
    ap.add_argument('--refresh-catalog',action='store_true')
    ap.add_argument('--resume')
    ap.add_argument('--db',default=str(Path(DATA_DIR)/'wiki/wiki.db'))
    ap.add_argument('--knowledge-db',default=str(Path(DATA_DIR)/'knowledge.db'))
    ap.add_argument('--cache-directory',default=str(Path(DATA_DIR)/'source-collection'))
    args=ap.parse_args()
    if args.resume and not args.live: ap.error('恢复任务须传--live')
    cache=Path(args.cache_directory)
    snapshot=refresh_catalog(Collector(cache),cache/'mcguide-catalog.json') if args.refresh_catalog else load_catalog(cache/'mcguide-catalog.json')
    store=WikiStore(args.db,create=False)
    if not args.live:
        plan=full_plan(store,args.knowledge_db,snapshot)
        print(json.dumps({'roles':plan['roles'],'errors':plan['errors']},ensure_ascii=False,indent=2))
        return int(bool(plan['errors']))
    report=sync_latest(store,args.knowledge_db,snapshot,root=store.path.parent/'latest-guides',cache_directory=cache,resume=args.resume,
                       progress=lambda e:print(json.dumps(e,ensure_ascii=False),flush=True))
    print(json.dumps({'run_id':report['run_id'],'state':report['state'],'summary':report['summary']},ensure_ascii=False))
    return 0 if report['state']=='completed' else 2


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
