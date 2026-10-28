"""Reviewed announcement changes and explainable review candidates, never automatic invalidation."""
from copy import deepcopy
import hashlib
import json
import re
from .store import WikiError

SCHEMA='notice-impacts-v1'
KINDS={'character_mechanics','weapon_effect','echo_effect','character_presentation','interaction_fix',
       'system_change','event_schedule','endgame_change','new_character','new_equipment'}
REVIEW_KINDS={'character_mechanics','weapon_effect','echo_effect','endgame_change','new_equipment'}
TABLES={'character':'characters','weapon':'weapons','echo':'echoes','sonata':'sonatas'}
POLICY=('只描述公告明确写出的新增、修复或调整，保留特定场景和触发条件。关联页只是应用筛选出的复核候选，'
        '不是官方认定其失效；needs_review不代表旧攻略错误、已削弱或已加强。显示/模型修复不等于战斗数值调整。'
        '没有匹配只表示本库已审核变更清单未覆盖，不证明公告没有或角色不受影响。'
        '本清单可能只覆盖公告部分内容。不能据此计算伤害、给出新版最优配队或把发布日期当实测日期。'
        '结论必须限定为“本条仅说明……，不能据此认定削弱/环境改变”，不得改说“你的角色没有变弱”或“环境没有改”。'
        '没有声明伤害数值调整不等于证明实战伤害完全不变；效果修复可能影响异常场景，影响大小未验证。'
        '缺少数值变更证据时，说“本条未声明数值调整”，不要说“不是数值改动”或“跟战斗数值没关系”。'
        '结尾也不能追加没有原文支持的安慰断言或细节；“模型表现异常”不能具体化为“衣服穿模”。维持角色卡身份，不扮演被查询的角色。'
        '不猜用户持有该角色；不展示内部ID、matched_changes、队列字段或状态枚举，用自然语言说明复核候选。'
        '已有复核记录时区分已核对待补证、所列结论无需文本修订、需要修订及已审核修订；不能一概说尚未核对，也不能说整篇已验证适用新版。'
        '活动日程为该版本公告的历史记录，是否正在进行须另查时间；不把历史活动说成当前可参加。')


def fingerprint(source):return hashlib.sha256(source['text'].encode()).hexdigest()


def candidates(source):
    """Unreviewed exact text units for operator inspection, never player evidence."""
    data=json.loads(source['text']);rows=[];section='';offset=0
    for line in data.get('readable_text','').splitlines(keepends=True):
        value=line.strip()
        if re.match(r'^(?:✦|●|【)',value):section=value
        if re.search(r'新增|修复了|优化|调整|活动时间|开放时间',value):
            rows.append({'offset':offset,'section':section,'quote':line.rstrip('\r\n'),'status':'needs_source_review'})
        offset+=len(line)
    return rows


def validate(source,page):
    review=page.get('notice_impacts')
    if review is None:return
    data=json.loads(source['text']);text=data.get('readable_text','')
    from .documents import iso_time
    if not data.get('source_date') or not iso_time(data['source_date']):raise WikiError('公告变更须有明确发布日期')
    if (data.get('kind')!='official_notice' or review.get('schema')!=SCHEMA
            or review.get('source_id')!=source['source_id'] or review.get('source_revision')!=source['revision']
            or review.get('source_sha256')!=fingerprint(source) or not data.get('game_version')
            or review.get('game_version')!=data['game_version'] or review.get('coverage')!='reviewed_spans_only'):
        raise WikiError('公告影响清单须锁定官方原文修订、哈希及明确版本，不能冒充整篇覆盖')
    entries=review.get('entries');seen=set()
    if not isinstance(entries,list) or not 1<=len(entries)<=80:raise WikiError('公告变更须有1至80条已核对原文')
    for entry in entries:
        key=entry.get('id');quote=entry.get('quote');start=entry.get('start')
        if (not isinstance(key,str) or not re.fullmatch(r'[a-z0-9-]{1,60}',key) or key in seen
                or entry.get('kind') not in KINDS or not isinstance(quote,str) or not 1<=len(quote)<=1600
                or type(start)!=int or start<0 or text[start:start+len(quote)]!=quote
                or start and text[start-1]!='\n' or start+len(quote)<len(text) and text[start+len(quote)]!='\n'):
            raise WikiError('变更须为完整原文行/段、唯一ID和明确分类')
        seen.add(key)
        if not isinstance(entry.get('subject'),str) or not entry['subject'] or entry['subject'] not in quote:
            raise WikiError('变更主体须在原文中出现')
        entities=entry.get('entities',[])
        if not isinstance(entities,list) or len(entities)>4:raise WikiError('变更实体关联超过范围')
        for entity in entities:
            if (entity.get('type') not in TABLES or not re.fullmatch(r'\d{1,16}',str(entity.get('id','')))
                    or not entity.get('name') or entity['name'] not in quote):
                raise WikiError('变更实体须有原文名称及明确类型ID')
    if any(e.get('entities') for e in entries):
        from .tools import KNOWLEDGE_PATH
        from contextlib import closing
        import sqlite3
        try:
            with closing(sqlite3.connect(KNOWLEDGE_PATH.resolve().as_uri()+'?mode=ro',uri=True)) as conn:
                validate_entities(review,conn)
        except (OSError,sqlite3.Error) as exc:raise WikiError('公告实体绑定暂无法校验') from exc


def validate_entities(review,conn):
    for entry in review['entries']:
        for entity in entry.get('entities',[]):
            row=conn.execute('SELECT name FROM '+TABLES[entity['type']]+' WHERE id=?',(int(entity['id']),)).fetchone()
            # Binding names may differ only by the Rover separator; no fuzzy matching.
            if not row or row[0].replace('·','・')!=entity['name'].replace('·','・'):
                raise WikiError('公告实体名称与本地ID不一致')


def build_page(source,page,manifest):
    result=deepcopy(page);result.pop('base_revision',None)
    result['notice_impacts']={k:deepcopy(v) for k,v in manifest.items() if k!='page_id'}
    validate(source,result);return result


def active_pages(store):
    """Published, clean and explicitly approved only; same gate as document search."""
    with store.connection() as conn:
        rows=conn.execute("""SELECT p.page_id,p.game_version,p.active_revision,r.payload FROM pages p
          JOIN page_revisions r ON r.page_id=p.page_id AND r.game_version=p.game_version AND r.locale=p.locale AND r.revision=p.active_revision
          WHERE p.locale='zh' AND p.dirty=0 AND EXISTS (
            SELECT 1 FROM drafts d JOIN reviews v ON v.draft_id=d.draft_id AND v.fingerprint=d.fingerprint
            WHERE d.page_id=p.page_id AND d.game_version=p.game_version AND d.locale=p.locale AND d.fingerprint=r.fingerprint
              AND d.state='published' AND v.review_id=(SELECT MAX(review_id) FROM reviews WHERE draft_id=d.draft_id) AND v.decision='approved')
          ORDER BY p.game_version,p.page_id LIMIT 2001""").fetchall()
    if len(rows)>2000:raise WikiError('已审核页面超过本轮扫描预算，请缩小索引范围')
    return [{**dict(r),'page':json.loads(r['payload'])} for r in rows]


def linked_pages(entry,pages):
    if entry['kind'] not in REVIEW_KINDS:return []
    entities={e['type']+':'+str(e['id']) for e in entry.get('entities',[])}
    equipment=[e['name'] for e in entry.get('entities',[]) if e['type'] in ('weapon','echo','sonata')]
    links=[]
    for row in pages:
        page=row['page'];meta=page.get('document_metadata',{})
        if meta.get('category') not in (None,'guide','endgame') or page.get('guide_quality',{}).get('status')=='pending_verification':continue
        ids=set(meta.get('entity_ids',page.get('entities',[])))
        direct=bool(ids&entities)
        mentions=[c for c in page['claims'] if any(name in c['text'] for name in equipment)]
        if not direct and not mentions:continue
        links.append({'page_id':row['page_id'],'revision':row['active_revision'],'namespace':row['game_version'],
            'title':page['title'],'entity_ids':sorted(ids),'relation':'same_entity' if direct else 'mentions_changed_equipment',
            'matching_claim_ids':[c['claim_id'] for c in mentions], 'status':'needs_review',
            'applicable_game_version':meta.get('applicable_game_version',row['game_version'] if row['game_version']!='documents-v1' else None),
            'note':'仅为复核候选；文字提及不代表推荐、持有或最佳配置。没有核实新版适用性，也未撤回或改写此页。'})
    return links


def search_impacts(store,*,game_version,entity=None,query='',limit=4,as_of=None):
    from .documents import iso_time
    from datetime import datetime,timezone
    pages=active_pages(store);found=[];now=iso_time(as_of) if as_of else datetime.now(timezone.utc).isoformat()
    from .impact_reviews import index as review_index,attach as attach_review,POLICY as review_policy
    assessments=review_index(store,as_of=now)
    for row in pages:
        page=row['page'];review=page.get('notice_impacts')
        if not review or review['game_version']!=game_version:continue
        source=store.get_source(review['source_id'],game_version='documents-v1')
        validate(source,page)
        if iso_time(page['document_metadata']['source_date'])>now:continue
        for entry in review['entries']:
            links=[attach_review(link,review['source_id'],entry['id'],assessments) for link in linked_pages(entry,pages)]
            keys={e['type']+':'+str(e['id']) for e in entry.get('entities',[])}
            if entity and entity not in keys and not any(entity in p['entity_ids'] for p in links):continue
            # Without an entity, require concrete query overlap except generic overview.
            overview=not query or bool(re.search(r'概览|汇总|全部|有哪些|主要|更新内容',query))
            if not entity and not overview and entry['subject'] not in query and not any(e['name'] in query for e in entry.get('entities',[])):continue
            local_links=links if not entity or entity in keys else [p for p in links if entity in p['entity_ids']]
            states={p['status'] for p in local_links}
            state=next((s for s in ('needs_review','revision_required','insufficient_evidence') if s in states),None)
            if state is None:state='reviewed_scoped' if states else 'needs_review' if entry['kind'] in REVIEW_KINDS else 'announcement_only'
            found.append({'id':entry['id'],'kind':entry['kind'],'subject':entry['subject'],'quote':entry['quote'],
                'entities':entry.get('entities',[]),'review_required':state in ('needs_review','revision_required','insufficient_evidence'),
                'related_pages':local_links[:3],'related_page_count':len(local_links),
                'source_id':source['source_id'],'source_revision':source['revision'],'url':page['document_metadata']['origin'],
                'source_date':page['document_metadata']['source_date'],'game_version':game_version,'title':source['title'],
                'assessment':state,
                'coverage':'reviewed_spans_only'})
    found.sort(key=lambda x:(not x['review_required'],x['source_id'],x['id']))
    total=len(found);selected=found[:limit]
    result={'route':'version_impacts','status':'found' if selected else 'no_reviewed_match','game_version':game_version,
            'results':selected,'matched_changes':total,'complete':False,'as_of':now,'policy':POLICY+review_policy}
    while True:
        result['trace']={'route':'version_impacts','entity':entity,'game_version':game_version,
                         'retriever_version':SCHEMA,'selected':[{'source_id':x['source_id'],'source_revision':x['source_revision'],'change_id':x['id']} for x in selected]}
        if not selected or len(json.dumps(result,ensure_ascii=False))<=7800:break
        selected.pop()
    result['status']='found' if selected else 'no_reviewed_match'
    return result
