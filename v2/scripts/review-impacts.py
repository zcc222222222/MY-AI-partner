"""Operator-only impact review lifecycle; read-only queue is the default."""
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
from llmwiki import impact_reviews as reviews
from llmwiki.updates import UpdateLock,atomic_json


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--db',default=str(WIKI_PATH));ap.add_argument('--output',required=True)
    actions=ap.add_mutually_exclusive_group();actions.add_argument('--stage',help='JSON array of pinned assessments');actions.add_argument('--decide',type=int);actions.add_argument('--revision',type=int)
    ap.add_argument('--decision',choices=['approved','rejected']);ap.add_argument('--reviewer');ap.add_argument('--note');ap.add_argument('--replacements',help='JSON claim_id to revised text map')
    args=ap.parse_args();store=WikiStore(args.db,create=False)
    if args.decide and not all((args.decision,args.reviewer,args.note)):ap.error('decisions require reviewer, decision and note')
    if args.revision and not args.replacements:ap.error('revision requires replacements')
    with UpdateLock(store.path):
        result={}
        if args.stage or args.decide or args.revision:
            backup=store.path.parent/'backups'/('before-impact-review-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.db');backup.parent.mkdir(parents=True,exist_ok=True)
            with closing(sqlite3.connect(store.path)) as original,closing(sqlite3.connect(backup)) as dest:original.backup(dest)
            result['backup']=str(backup)
        if args.stage:
            records=json.loads(Path(args.stage).read_text(encoding='utf-8'))
            if not isinstance(records,list) or not 1<=len(records)<=20:ap.error('batch must contain 1 to 20 records')
            result['staged']=[]
            for record in records:
                result['staged'].append(reviews.stage(store,record));atomic_json(args.output,result)
        elif args.decide:
            result['decision']=reviews.decide(store,args.decide,decision=args.decision,reviewer=args.reviewer,note=args.note)
        elif args.revision:
            result['draft']=reviews.stage_revision(store,args.revision,json.loads(Path(args.replacements).read_text(encoding='utf-8')))
        result['queue']=reviews.queue(store);atomic_json(args.output,result)
    print(json.dumps({k:v for k,v in result.items() if k!='queue'},ensure_ascii=False));print(json.dumps(result['queue']['counts']))
    return 1 if result.get('draft',{}).get('valid') is False else 0


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');raise SystemExit(main())
