"""Reviewed phase roles and local alternatives, anchored to unchanged source rows."""
import re
from copy import deepcopy
from .store import WikiError

PHASE_WORDS={'opening':r'起手|启动|开场|开局','amplification':r'增伤',
             'burst':r'爆发','cycle':r'循环'}


def validate_paragraph(block,raw):
    """Review anchors identify ordered source text; they do not synthesize steps."""
    anchors=block.get('sequence_anchors')
    if (block['phase']!='原文流程' or block.get('prose') is not True
            or len(raw)<12 or not isinstance(anchors,list) or not 3<=len(anchors)<=12):
        raise WikiError('无标题文字流程须使用应用标签并保留至少三个原文顺序锚点')
    end=-1
    for anchor in anchors:
        if not isinstance(anchor,str) or len(anchor)<2 or raw.count(anchor)!=1:
            raise WikiError('文字动作锚点缺失或不唯一')
        start=raw.index(anchor)
        if start<end:raise WikiError('文字动作锚点重叠或被重排')
        end=start+len(anchor)


def attach_structure(spec,parts):
    scope=spec.get('coverage_scope')
    if scope is not None and scope!='local_moves':raise WikiError('未知的局部流程范围')
    notation=spec.get('notation')
    if notation is not None:
        quote=notation.get('quote','')
        if (notation.get('case_sensitive') is not True or not quote or quote not in spec.get('context',[])
                or not any(re.search(r'(?<![A-Za-z])'+c+r'[-：]',quote) and re.search(r'(?<![A-Za-z])'+c.upper()+r'[-：]',quote) for c in 'abcdefghijklmnopqrstuvwxyz')):
            raise WikiError('大小写键位说明须保留同一原文上下文里的大小写定义')
    for block,part in zip(spec['blocks'],parts):
        cycle=block.get('cycle_reference')
        if cycle is not None:
            quote=cycle.get('quote','');kind=cycle.get('kind')
            cue={'start_condition_only':r'开始(?:进入)?循环','order_only':r'顺序循环',
                 'unspecified_reuse':r'后续循环一样|重复','partial_ellipsis':r'\.{3,}|…'}.get(kind)
            if not cue or not quote or part['raw'].count(quote)!=1 or not re.search(cue,quote):
                raise WikiError('循环引用须保留唯一的原文起点或顺序说明')
            part['cycle_reference']={**deepcopy(cycle),'expanded':False}
        role=block.get('phase_role')
        if role is not None:
            if role not in PHASE_WORDS or not re.search(PHASE_WORDS[role],part['phase']):
                raise WikiError('阶段分类缺少该阶段原文名称支持')
            part['phase_role']=role
        branches=block.get('branches')
        if branches is not None:
            if scope!='local_moves' or not isinstance(branches,list) or len(branches)<2:
                raise WikiError('条件分支须绑定局部流程并保留至少两条原文')
            rows=[];conditions=[]
            for branch in branches:
                line=branch['line'];condition=branch['condition'];outcome=branch['outcome']
                if (not condition or not outcome or part['raw'].splitlines().count(line)!=1
                        or not re.fullmatch(r'.*'+re.escape(condition)+'→'+re.escape(outcome)+r'[。；;]?',line)):
                    raise WikiError('条件与结果未在同一完整原文行中直接相连')
                rows.append(line);conditions.append(condition)
            if (len(set(conditions))!=len(conditions) or rows!=[l for l in block['lines'] if '→' in l]):
                raise WikiError('条件分支被遗漏、重复或重排')
            part['branches']=deepcopy(branches)
    return parts


def unexpanded_cycle_request(spec,query):
    return any(b.get('cycle_reference') for b in spec.get('blocks',[])) and bool(
        re.search(r'完整.{0,8}循环|循环轴|展开.{0,8}循环|闭合.{0,8}循环',query))


def local_scope_mismatch(spec,query):
    return spec.get('coverage_scope')=='local_moves' and bool(re.search(
        r'完整.{0,8}(?:轴|轮转|循环)|整套|全队|启动轴|循环轴|开局攻击轴',query))
