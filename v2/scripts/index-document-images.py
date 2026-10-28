"""Rebuild image associations from immutable local source revisions; no network/model."""
import argparse
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from knowledge.config import DATA_DIR
from llmwiki import WikiStore
from llmwiki.updates import UpdateLock, source_snapshot
from llmwiki.images import extract_images, index_source_images


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--write',action='store_true')
    ap.add_argument('--db',default=str(Path(DATA_DIR)/'wiki/wiki.db'))
    args=ap.parse_args()
    store=WikiStore(args.db,create=False)
    counts={}
    with UpdateLock(store.path):
        if args.write:
            path=store.path.parent/'backups'/('before-image-index-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.db')
            path.parent.mkdir(parents=True,exist_ok=True)
            with closing(sqlite3.connect(store.path)) as original,closing(sqlite3.connect(path)) as backup: original.backup(backup)
        sources=[store.get_source(sid,game_version='documents-v1') for sid in source_snapshot(store)]
        with store.connection(write=args.write) as c:
            if args.write: c.execute('DELETE FROM document_images')
            for source in sources:
                if args.write:
                    count=index_source_images(c,source['source_id'],source['revision'],source['text'],source['origin'])
                else:
                    count=len(extract_images(source['source_id'],source['revision'],json.loads(source['text']),source['origin']))
                counts[source['source_id']]=count
    print(json.dumps({'written':args.write,'sources':len(counts),'images':sum(counts.values()),'sources_with_images':sum(n>0 for n in counts.values())},ensure_ascii=False))


if __name__=='__main__': main()
