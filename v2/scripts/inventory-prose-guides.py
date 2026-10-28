"""Replay a reviewed, pinned prose inventory without crawling or auto-promoting hints."""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from llmwiki import WikiStore
from llmwiki.tools import WIKI_PATH
from llmwiki.guide_curation import source_text,fingerprint


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('review');ap.add_argument('--db',default=str(WIKI_PATH));ap.add_argument('--output',required=True);args=ap.parse_args()
    review=json.loads(Path(args.review).read_text(encoding='utf-8'));store=WikiStore(args.db,create=False);rows=[]
    for item in review['sources']:
        src=store.get_source(item['source_id'],game_version='documents-v1')
        if src['revision']!=item['source_revision'] or fingerprint(src)!=item['source_sha256']:raise ValueError('来源已更新，不能复用旧盘点结论: '+item['source_id'])
        doc=json.loads(src['text']);fields=[]
        for i,block in enumerate(doc['content'].get('baseTexts',[])):
            if block.get('language')!='zh-Hans':continue
            for key,value in block.items():
                if key=='language' or not isinstance(value,str) or not value.strip():continue
                path=['content','baseTexts',i,key];text=source_text(src,path)
                hints=[{'line':n+1,'text':line} for n,line in enumerate(text.splitlines()) if re.search('操作|流程|连招|起手|变奏入场|切|长按|循环|协奏|➔|->|＋',line)]
                fields.append({'source_path':path,'text':text,'candidate_lines':hints})
        rows.append({**item,'title':doc['title'],'text_fields':fields})
    result={'schema':'prose-inventory-v1','scope':'29 sources missed by the prior arrow-only introductionDetail inventory',
            'note':'人工决定只覆盖每项reviewed_paths；其余字段的候选行是线索，不证明流程完整或事实正确。未做OCR和视频识别，不自动发布。',
            'source_count':len(rows),'decision_counts':dict(Counter(r['decision'] for r in rows)),'sources':rows}
    out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({k:v for k,v in result.items() if k!='sources'},ensure_ascii=False))


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');main()
