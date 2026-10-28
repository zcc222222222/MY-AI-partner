"""Pinned follow-up checks explain open gaps without clearing source quality holds."""
from copy import deepcopy
import json
from urllib.parse import urlsplit
from .store import WikiError

SCHEMA='guide-followup-v1'
KINDS={'source_unchanged','source_refresh_failed','local_crosscheck','external_reference'}


def validate_followup(source,page):
    review=page.get('guide_followup')
    if review is None:return
    from .guide_curation import fingerprint,source_text
    from .documents import iso_time
    if page.get('document_metadata',{}).get('source_id')!=source['source_id']:
        raise WikiError('不能将补证审核应用到另一篇攻略')
    if (review.get('schema')!=SCHEMA or review.get('status') not in ('open','resolved')
            or review.get('source_id')!=source['source_id'] or review.get('source_revision')!=source['revision']
            or review.get('source_sha256')!=fingerprint(source)):
        raise WikiError('补证审核必须锁定原篇且不能自动解除限制')
    if review['status']=='open' and review.get('original_issue_resolved') is not False:
        raise WikiError('开放补证不能声称原问题已解决')
    if review['status']=='resolved':
        prior=review.get('supersedes_source_revision')
        reviewed_flow=(page.get('guide_curation',{}).get('coverage_review',{}).get('status')=='reviewed_text'
                       and bool(page.get('guide_curation',{}).get('rotations')))
        if (type(prior)!=int or not 1<=prior<source['revision'] or review.get('original_issue_resolved') is not True
                or not isinstance(review.get('resolution'),str) or not review['resolution'].strip()
                or not (page.get('guide_quality',{}).get('status')=='resolved' or reviewed_flow)):
            raise WikiError('关闭补证须有原篇新修订、明确复核说明及已核对的修复内容，历史补充不能代替')
    try:iso_time(review['checked_at'])
    except (ValueError,KeyError,TypeError):raise WikiError('补证审核缺少有效核对日期')
    checks=review.get('checks')
    if not isinstance(checks,list) or not 1<=len(checks)<=8:raise WikiError('补证审核须有明确检查记录')
    for check in checks:
        if check.get('kind') not in KINDS or not isinstance(check.get('note'),str) or not 1<=len(check['note'])<=400:
            raise WikiError('未知补证检查或缺少结论')
        if check.get('kind')=='local_crosscheck':
            if not check.get('quote') or check['quote'] not in source_text(source,check['source_path']):
                raise WikiError('本地补证未绑定原文')
        if check.get('kind')=='source_unchanged':
            import hashlib
            digest=hashlib.sha256(json.dumps(json.loads(source['text'])['content'],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
            if check.get('content_sha256')!=digest:raise WikiError('未变化结论未绑定已核对正文哈希')
        if check.get('kind')=='external_reference' and (not check.get('url') or not check.get('title') or check.get('scope') not in ('mechanism_crosscheck_only','historical_candidate')):
            raise WikiError('外部补证须标明页面与有限适用范围')
        if check.get('url'):
            url=urlsplit(check['url'])
            if url.scheme!='https' or url.hostname not in ('wiki.biligame.com','www.taptap.cn','guide-server.aki-game.com') or url.username or url.password or url.port:
                raise WikiError('补证引用须为审核范围内的公开页面')
    refs=review.get('historical_pages',[])
    if not isinstance(refs,list) or len(refs)>2:raise WikiError('历史补充引用超过范围')
    for ref in refs:
        if (ref.get('page_id')==page['page_id'] or type(ref.get('revision'))!=int or not ref.get('game_version')
                or {'page_id':ref['page_id'],'revision':ref['revision']} not in page.get('pages',[])):
            raise WikiError('历史补充须绑定独立页面修订和版本')


def validate_targets(conn,page,scope):
    for ref in page.get('guide_followup',{}).get('historical_pages',[]):
        row=conn.execute('SELECT payload FROM page_revisions WHERE page_id=? AND game_version=? AND locale=? AND revision=?',
                         (ref['page_id'],*scope,ref['revision'])).fetchone()
        meta=json.loads(row[0]).get('document_metadata',{}) if row else {}
        if (meta.get('source_role')!='historical_supplement' or meta.get('supplement_for')!=page['document_metadata']['source_id']
                or any(ref.get(k)!=meta.get(v) for k,v in (('game_version','applicable_game_version'),('url','origin'),('author','author')))):
            raise WikiError('历史补充的关联原篇、版本或出处不一致')


def validate_transition(previous,page):
    if previous.get('guide_followup') and not page.get('guide_followup'):
        raise WikiError('普通重编译不能删除补证审核记录')
    old=previous.get('guide_followup');new=page.get('guide_followup')
    if old and new and old['status']=='open' and new['status']=='resolved' and new.get('supersedes_source_revision')!=old['source_revision']:
        raise WikiError('补证关闭未对应原开放审核的来源修订')


def build_followup_page(source,page,manifest):
    result=deepcopy(page);result.pop('base_revision',None)
    review={k:deepcopy(v) for k,v in manifest.items() if k!='page_id'}
    result['guide_followup']=review
    for ref in review.get('historical_pages',[]):
        dep={'page_id':ref['page_id'],'revision':ref['revision']}
        result['pages']=[p for p in result.get('pages',[]) if p['page_id']!=ref['page_id']]+[dep]
    validate_followup(source,result)
    return result


def public_context(page):
    review=page['guide_followup']
    return {'schema':SCHEMA,'status':review['status'],'original_issue_resolved':review['original_issue_resolved'],'checked_at':review['checked_at'],
            'checks':[{k:c[k] for k in ('kind','note','url','title','scope') if k in c} for c in review['checks']],
            'historical_pages':deepcopy(review.get('historical_pages',[])),
            'note':('本次仅对原篇新修订完成明确复核，不表示其他版本或整篇全部事实已验证。' if review['status']=='resolved' else '补证记录不解除原篇质量或覆盖限制。')+'跨来源机制对照不能改写作者链数边界。历史补充是另一位作者、另一版本的独立资料；用户指定相应版本或明确要历史参考时另查，不能冒充当前攻略或本篇原文。刷新失败不等于确认未变化。'}
