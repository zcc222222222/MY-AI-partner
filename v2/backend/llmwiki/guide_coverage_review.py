"""Pinned text review boundaries, distinct from gameplay validation."""
from copy import deepcopy
from .store import WikiError


def validate_review(source,review):
    from .guide_curation import source_text
    if not isinstance(review,dict) or review.get('status') not in ('reviewed_text','no_action_sequence','ambiguous_chain_boundary'):
        raise WikiError('未知攻略覆盖审核状态')
    paths=review.get('reviewed_paths',[])
    if not paths or not isinstance(review.get('note'),str) or not review['note'].strip():
        raise WikiError('覆盖审核缺少范围或说明')
    for path in paths:
        if (len(path)!=4 or path[:2]!=['content','baseTexts'] or type(path[2])!=int
                or path[3] not in ('introductionDetail','introductionSynopsis','skillDisplayDescription')):
            raise WikiError('覆盖审核只支持明确的中文攻略文字字段')
        source_text(source,path)
    blockers=review.get('blockers',[])
    if any(b.get('source_path') not in paths or not b.get('quote') or b['quote'] not in source_text(source,b['source_path']) for b in blockers):
        raise WikiError('覆盖限制未绑定审核字段原文')
    if review['status']=='ambiguous_chain_boundary' and (len(blockers)<2 or not any('三链以上' in b['quote'] for b in blockers) or not any('三链以下' in b['quote'] for b in blockers)):
        raise WikiError('链数边界重叠缺少两侧原文')


def public_review(review):
    return {k:deepcopy(review[k]) for k in ('status','reviewed_paths','note')}
