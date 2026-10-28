"""Source-pinned quality holds; publication review and factual reliability differ."""
from copy import deepcopy
import hashlib
import json
from .collector import html_text
from .store import WikiError

SCHEMA='guide-quality-v1'
CODES={'skill_attribution','key_binding','unsupported_numeric','buff_condition'}


def fingerprint(source):return hashlib.sha256(source['text'].encode()).hexdigest()


def validate_quality(source,page):
    review=page.get('guide_quality')
    if review is None:return
    if (page['document_metadata']['category']!='guide' or review.get('schema')!=SCHEMA
            or review.get('source_id')!=source['source_id'] or review.get('source_revision')!=source['revision']
            or review.get('source_sha256')!=fingerprint(source)
            or review.get('status') not in ('pending_verification','resolved')):
        raise WikiError('攻略质量标记与来源修订不一致')
    issues=review.get('issues')
    if not isinstance(issues,list) or not 1<=len(issues)<=12:raise WikiError('质量标记须保留明确的原文问题锚点')
    seen=set()
    for issue in issues:
        value=json.loads(source['text'])
        if issue.get('code') not in CODES or not issue.get('source_path') or not isinstance(issue.get('quote'),str) or not issue['quote']:
            raise WikiError('未知质量问题或缺少原文')
        for key in issue['source_path']:value=value[key]
        if not isinstance(value,str) or issue['quote'] not in html_text(value):raise WikiError('质量问题锚点不在锁定原文中')
        identity=json.dumps(issue,ensure_ascii=False,sort_keys=True)
        if identity in seen:raise WikiError('重复质量问题')
        seen.add(identity)
    if review['status']=='resolved':
        previous=review.get('supersedes_source_revision')
        if (type(previous)!=int or not 1<=previous<source['revision']
                or not isinstance(review.get('resolution'),str) or not review['resolution'].strip()):
            raise WikiError('解除标记须对应更新后的来源及明确复核说明')


def validate_transition(previous,page):
    old=previous.get('guide_quality');new=page.get('guide_quality')
    if old and not new:raise WikiError('普通重编译不能删除既有质量审核标记')
    if old and new and old['status']=='pending_verification' and new['status']=='resolved':
        if new.get('supersedes_source_revision')!=old['source_revision']:
            raise WikiError('解除标记未对应此前待核对的来源修订')


def is_held(page):return page.get('guide_quality',{}).get('status')=='pending_verification'


def public_context(page):
    review=page.get('guide_quality')
    if not review:return {}
    return {'status':review['status'],'source_revision':review['source_revision'],
            'issue_types':sorted({i['code'] for i in review['issues']}),
            'note':('本篇有待核对内容，本次仅返回来源信息。不要依据本篇给出机制、数值、配队或完整手法建议，不转述问题原文作为结论。可按用户明确版本另查机制，不能从攻略日期推断版本，也不能把其他来源结论说成已修复本篇。'
                    if is_held(page) else '质量复核仅对应这个更新后的来源修订，仍须区分作者观点与机制证据；不表示整篇全部事实或当前版本适用性得到证明。')}


def build_quality_page(source,page,manifest):
    if page.get('document_metadata',{}).get('source_id')!=source['source_id']:
        raise WikiError('不能把质量标记应用到另一篇攻略')
    result=deepcopy(page);result.pop('base_revision',None)
    result['guide_quality']={k:deepcopy(v) for k,v in manifest.items() if k!='page_id'}
    validate_quality(source,result);validate_transition(page,result)
    return result
