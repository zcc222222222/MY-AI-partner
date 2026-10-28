"""Reviewed identity labels; provenance is separate from native source aliases."""
import json
from pathlib import Path


def character_aliases(entity_id,name):
    rows=json.loads(Path(__file__).with_name('reviewed_character_aliases.json').read_text(encoding='utf-8'))
    return [{'alias':r['alias'],'entity_type':'character','entity_id':str(entity_id),'locale':'zh',
             'source':'reviewed_source:'+r['source_id']+'@'+str(r['source_revision'])}
            for r in rows if r['entity_id']==str(entity_id) and r['canonical_name']==name]
