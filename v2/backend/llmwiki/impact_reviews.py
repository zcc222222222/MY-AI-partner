"""Append-only, explicitly approved impact assessments in the existing Wiki audit log."""
from copy import deepcopy
from datetime import datetime,timezone
import hashlib
import json
from .store import WikiError

SCHEMA='impact-assessment-v1'
STAGED='impact_assessment_staged'
DECIDED='impact_assessment_decided'
OUTCOMES={'no_text_change','insufficient_evidence','revision_required'}
POLICY=('复核结论只对列出的结论及锁定修订有效；无需文本修订不等于整篇或实战已验证适用新版。'
        '证据不足表示已核对但仍待补证，不表示没有风险或攻略错误。任何一侧来源、页面或审核状态变化后须重新复核。')


def sha(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def approved(conn,row):
    return bool(conn.execute("""SELECT 1 FROM drafts d JOIN reviews v ON v.draft_id=d.draft_id AND v.fingerprint=d.fingerprint
      WHERE d.page_id=? AND d.game_version=? AND d.locale='zh' AND d.fingerprint=? AND d.state='published'
      AND v.review_id=(SELECT MAX(review_id) FROM reviews WHERE draft_id=d.draft_id) AND v.decision='approved'""",
      (row['page_id'],row['game_version'],row['fingerprint'])).fetchone())


def snapshot(conn,page_id,namespace):
    row=conn.execute("""SELECT p.page_id,p.game_version,p.active_revision,p.dirty,r.payload,r.fingerprint FROM pages p
      JOIN page_revisions r ON r.page_id=p.page_id AND r.game_version=p.game_version AND r.locale=p.locale AND r.revision=p.active_revision
      WHERE p.page_id=? AND p.game_version=? AND p.locale='zh'""",(page_id,namespace)).fetchone()
    if not row or row['dirty'] or not approved(conn,row):raise WikiError('复核对象未发布、已失效或未通过审核')
    page=json.loads(row['payload']);sources=[]
    for ref in page['sources']:
        source=conn.execute("""SELECT r.revision,r.text,r.deleted FROM sources s JOIN source_revisions r
          ON r.source_id=s.source_id AND r.game_version=s.game_version AND r.locale=s.locale AND r.revision=s.current_revision
          WHERE s.source_id=? AND s.game_version=? AND s.locale='zh'""",(ref['source_id'],namespace)).fetchone()
        if not source or source['deleted'] or source['revision']!=ref['revision']:raise WikiError('复核依赖的原文已变化或撤回')
        sources.append({'source_id':ref['source_id'],'revision':ref['revision'],'sha256':hashlib.sha256(source['text'].encode()).hexdigest()})
    return {'page_id':page_id,'namespace':namespace,'revision':row['active_revision'],'sha256':sha(page),'sources':sources},page


def key(record):
    return (record['notice']['source_id'],record['notice']['change_id'],record['target']['namespace'],record['target']['page_id'])


def template(store,notice_page,change_id,target_page,namespace):
    from .notice_impacts import linked_pages,active_pages
    pages=active_pages(store)
    with store.connection() as conn:
        notice_pin,notice=snapshot(conn,notice_page,'documents-v1');target_pin,target=snapshot(conn,target_page,namespace)
    entry=next((e for e in notice.get('notice_impacts',{}).get('entries',[]) if e['id']==change_id),None)
    if not entry:raise WikiError('变更不在已审核公告清单中')
    if not any(p['page_id']==target_page and p['namespace']==namespace for p in linked_pages(entry,pages)):
        raise WikiError('页面不是该变更的复核候选')
    review=notice['notice_impacts']
    return {'schema':SCHEMA,'notice':{'source_id':review['source_id'],'game_version':review['game_version'],
        'change_id':change_id,'change_sha256':sha(entry),'page':notice_pin},'target':target_pin,
        'coverage':'listed_claims_only','checks':[],'summary':'','kind':'assessment'}


def outcome(record):
    if record.get('kind')=='revision_published':return 'revision_published'
    values={c['outcome'] for c in record['checks']}
    return 'revision_required' if 'revision_required' in values else 'insufficient_evidence' if 'insufficient_evidence' in values else 'no_text_change'


def validate(conn,record):
    if (record.get('schema')!=SCHEMA or record.get('coverage')!='listed_claims_only'
            or record.get('kind') not in ('assessment','revision_published')
            or not isinstance(record.get('summary'),str) or not 1<=len(record['summary'])<=400):
        raise WikiError('复核须有明确范围、结论及协议版本')
    n=record['notice'];notice_pin,notice=snapshot(conn,n['page']['page_id'],'documents-v1')
    target_pin,target=snapshot(conn,record['target']['page_id'],record['target']['namespace'])
    if notice_pin!=n['page'] or target_pin!=record['target']:raise WikiError('复核的公告或目标页面/原文修订已变化')
    review=notice.get('notice_impacts',{})
    entry=next((e for e in review.get('entries',[]) if e['id']==n['change_id']),None)
    if (not entry or review['source_id']!=n['source_id'] or review['game_version']!=n['game_version'] or sha(entry)!=n['change_sha256']):
        raise WikiError('公告变更条目已变化，须重新复核')
    if target.get('guide_quality',{}).get('status')=='pending_verification':raise WikiError('不能用公告复核清除原篇质量挂起')
    # Recheck the actual relation inside the write transaction; no caller-supplied target association.
    from .notice_impacts import linked_pages
    if not linked_pages(entry,[{'page_id':target_pin['page_id'],'game_version':target_pin['namespace'],
                                'active_revision':target_pin['revision'],'page':target}]):
        raise WikiError('目标与公告变更没有已审核文本关联')
    checks=record.get('checks');claims={c['claim_id']:c for c in target['claims']};seen=set()
    if not isinstance(checks,list) or not 1<=len(checks)<=300:raise WikiError('复核须逐项绑定目标结论')
    for check in checks:
        cid=check.get('claim_id')
        if (cid not in claims or cid in seen or check.get('claim_sha256')!=sha(claims[cid])
                or check.get('outcome') not in OUTCOMES or not isinstance(check.get('note'),str) or not 1<=len(check['note'])<=400):
            raise WikiError('复核项未锁定结论或缺少有效核对说明')
        seen.add(cid)
    if record['kind']=='revision_published':
        old=load_approved(conn,record.get('supersedes'))
        if (not old or outcome(old)!='revision_required' or key(old)!=key(record) or old['notice']!=record['notice']
                or old['target']['revision']>=record['target']['revision'] or outcome({**record,'kind':'assessment'})!='no_text_change'):
            raise WikiError('修订完成须对应同一公告的修订要求，并有已审核的新目标页面')
        prior=conn.execute('SELECT payload FROM page_revisions WHERE page_id=? AND game_version=? AND locale=? AND revision=?',
                           (old['target']['page_id'],old['target']['namespace'],'zh',old['target']['revision'])).fetchone()
        previous={c['claim_id']:c['text'] for c in json.loads(prior[0])['claims']} if prior else {}
        required={c['claim_id'] for c in old['checks'] if c['outcome']=='revision_required'}
        if any(cid not in seen or cid not in claims or previous.get(cid)==claims[cid]['text'] for cid in required):
            raise WikiError('被要求修订的结论未逐项修改并复核，不能仅抬高页面修订号')


def events(conn):
    rows=conn.execute('SELECT event_id,kind,payload,created_at FROM events WHERE kind IN (?,?) ORDER BY event_id LIMIT 5001',(STAGED,DECIDED)).fetchall()
    if len(rows)>5000:raise WikiError('复核审计超过单次读取预算，需分页归档')
    return [{**dict(r),'payload':json.loads(r['payload'])} for r in rows]


def load_approved(conn,stage_id):
    if type(stage_id)!=int:return None
    rows=events(conn);stage=next((e for e in rows if e['event_id']==stage_id and e['kind']==STAGED),None)
    decisions=[e for e in rows if e['kind']==DECIDED and e['payload']['stage_id']==stage_id]
    if not stage or not decisions or decisions[-1]['payload']['decision']!='approved':return None
    if stage['payload']['fingerprint']!=sha(stage['payload']['record']) or decisions[-1]['payload']['fingerprint']!=stage['payload']['fingerprint']:
        return None
    return stage['payload']['record']


def emit(conn,kind,payload,version):
    return conn.execute('INSERT INTO events(kind,game_version,locale,payload,created_at) VALUES(?,?,?,?,?)',
        (kind,version,'zh',json.dumps(payload,ensure_ascii=False,sort_keys=True),datetime.now(timezone.utc).isoformat())).lastrowid


def stage(store,record):
    with store.connection(write=True) as conn:
        validate(conn,record);fingerprint=sha(record)
        for e in reversed(events(conn)):
            if e['kind']==STAGED and e['payload']['fingerprint']==fingerprint:
                return {'stage_id':e['event_id'],'unchanged':True,'outcome':outcome(record)}
        sid=emit(conn,STAGED,{'record':record,'fingerprint':fingerprint},record['notice']['game_version'])
    return {'stage_id':sid,'unchanged':False,'outcome':outcome(record)}


def decide(store,stage_id,*,decision,reviewer,note):
    if decision not in ('approved','rejected') or not reviewer.strip() or not note.strip():raise WikiError('须填写显式审核人和说明')
    with store.connection(write=True) as conn:
        rows=events(conn);staged=next((e for e in rows if e['kind']==STAGED and e['event_id']==stage_id),None)
        if not staged:raise WikiError('复核草稿不存在')
        record=staged['payload']['record']
        if decision=='approved':
            if staged['payload']['fingerprint']!=sha(record):raise WikiError('复核草稿指纹不一致')
            validate(conn,record)
        previous=[e for e in rows if e['kind']==DECIDED and e['payload']['stage_id']==stage_id]
        if previous and previous[-1]['payload']['decision']==decision:return {'event_id':previous[-1]['event_id'],'unchanged':True}
        eid=emit(conn,DECIDED,{'stage_id':stage_id,'decision':decision,'reviewer':reviewer,'note':note,
                            'fingerprint':staged['payload']['fingerprint']},record['notice']['game_version'])
        generation=store._bump(conn)
    return {'event_id':eid,'unchanged':False,'generation':generation}


def index(store,as_of=None):
    with store.connection() as conn:
        rows=events(conn);stages={e['event_id']:e for e in rows if e['kind']==STAGED};latest={}
        for event in rows:
            if event['kind']!=DECIDED or as_of and event['created_at']>as_of:continue
            item=event['payload'];s=stages.get(item['stage_id'])
            if s:latest[key(s['payload']['record'])]=(s,event)
        result={}
        for k,(s,event) in latest.items():
            record=s['payload']['record'];state='needs_review';reason=None
            integrity=s['payload']['fingerprint']==sha(record) and event['payload']['fingerprint']==s['payload']['fingerprint']
            if not integrity:reason='复核审计指纹不一致，先前结论已停止使用。'
            elif event['payload']['decision']=='approved':
                try:validate(conn,record);state=outcome(record)
                except (WikiError,KeyError,ValueError,TypeError):reason='公告、目标、原文或审核状态发生变化，先前结论已停止使用。'
            else:reason='先前复核已撤回，须重新核对。'
            result[k]={'status':state,'stage_id':s['event_id'],'decided_at':event['created_at'],'reopened':state=='needs_review',
                'summary':record['summary'] if state!='needs_review' else reason,'checks':record['checks'] if state!='needs_review' else [],
                'target_revision':record['target']['revision'],'record':record}
    return result


def attach(link,notice_source,change_id,records):
    k=(notice_source,change_id,link['namespace'],link['page_id']);review=records.get(k)
    if not review:return link
    result=deepcopy(link);result['status']=review['status']
    result['assessment_review']={k:review[k] for k in ('status','stage_id','decided_at','reopened','summary','target_revision')}
    result['assessment_review']['checked_claims']=[{'claim_id':c['claim_id'],'outcome':c['outcome']} for c in review['checks']]
    result['assessment_review']['coverage']='listed_claims_only'
    result['assessment_review']['note']=POLICY
    return result


def stage_revision(store,stage_id,replacements):
    """Concrete edits only to flagged claims; normal Wiki lint/review/publish still applies."""
    with store.connection() as conn:
        record=load_approved(conn,stage_id)
        if not record or outcome(record)!='revision_required':raise WikiError('须先批准需要修订的复核结论')
        validate(conn,record);_,page=snapshot(conn,record['target']['page_id'],record['target']['namespace'])
    required={c['claim_id'] for c in record['checks'] if c['outcome']=='revision_required'}
    if not isinstance(replacements,dict) or set(replacements)!=required:raise WikiError('仅可逐项修订本次明确标记的结论')
    edited=deepcopy(page);edited['base_revision']=record['target']['revision']
    for claim in edited['claims']:
        if claim['claim_id'] not in required:continue
        old=claim['text'];new=replacements[claim['claim_id']]
        if not isinstance(new,str) or not new.strip() or old==new or edited['body'].count(old)!=1:
            raise WikiError('修订文本须有实际变化且原段落位置唯一')
        edited['body']=edited['body'].replace(old,new,1);claim['text']=new
    edited['impact_revision_parent']=stage_id
    return store.stage_page(edited)


def queue(store):
    from .notice_impacts import active_pages,linked_pages
    pages=active_pages(store);records=index(store);items=[];present=set()
    for row in pages:
        review=row['page'].get('notice_impacts')
        if not review:continue
        for entry in review['entries']:
            for link in linked_pages(entry,pages):
                item=attach(link,review['source_id'],entry['id'],records)
                item.update(source_id=review['source_id'],change_id=entry['id'],game_version=review['game_version'])
                items.append(item);present.add((review['source_id'],entry['id'],link['namespace'],link['page_id']))
    # Preserve reopen alerts even when a changed notice is temporarily dirty and cannot be queried.
    for k,review in records.items():
        if k not in present and review['reopened']:
            items.append({'source_id':k[0],'change_id':k[1],'namespace':k[2],'page_id':k[3],
                          'status':'needs_review','reopened':True,'note':review['summary']})
    counts={s:sum(x['status']==s for x in items) for s in ('needs_review','no_text_change','insufficient_evidence','revision_required','revision_published')}
    return {'schema':'impact-review-queue-v1','items':items,'counts':counts,'policy':POLICY}


def page_summaries(store,results,namespace,as_of=None):
    """Carry scoped review warnings into ordinary retrieval, without changing original claims."""
    from .notice_impacts import active_pages,linked_pages
    from .documents import iso_time
    moment=as_of or datetime.now(timezone.utc).isoformat();records=index(store,as_of=moment)
    pages=active_pages(store);targets={(p['game_version'],p['page_id']):p for p in pages}
    for result in results:
        target=targets.get((namespace,result['page_id']))
        if not target:continue
        delivered={c['claim_id'] for c in result.get('matches',[])};summaries=[]
        for carrier in pages:
            review=carrier['page'].get('notice_impacts');meta=carrier['page'].get('document_metadata',{})
            if not review or not meta.get('source_date') or iso_time(meta['source_date'])>moment:continue
            for entry in review['entries']:
                links=linked_pages(entry,[target])
                if not links:continue
                state=records.get((review['source_id'],entry['id'],namespace,result['page_id']))
                if state and state['checks'] and not delivered&{c['claim_id'] for c in state['checks']}:continue
                link=attach(links[0],review['source_id'],entry['id'],records)
                if state and state['checks']:
                    matching=[c for c in state['checks'] if c['claim_id'] in delivered]
                    scoped=state['status'] if state['status']=='revision_published' else outcome({'kind':'assessment','checks':matching})
                    link['status']=scoped;link['assessment_review']['status']=scoped
                    link['assessment_review']['record_status']=state['status']
                    link['assessment_review']['checked_claims']=[{'claim_id':c['claim_id'],'outcome':c['outcome'],'note':c['note']} for c in matching[:6]]
                summaries.append({'change_id':entry['id'],'subject':entry['subject'],'quote':entry['quote'],
                    'status':link['status'],'review':link.get('assessment_review'),
                    'notice_source':{'source_id':review['source_id'],'revision':review['source_revision'],
                                     'url':meta['origin'],'date':meta['source_date'],'game_version':review['game_version'],'title':carrier['page']['title']},
                    'note':POLICY})
        summaries.sort(key=lambda s:(s['status'] not in ('needs_review','revision_required','insufficient_evidence'),s['change_id']))
        if summaries:result['impact_reviews']=summaries[:2]
