"""Explicit owner-chain ranges and local reuse edges; never infer actions."""
import re
from .store import WikiError

NUM=r'(?:[0-9]+|[零一二三四五六七八九十两]+)'
CN='零一二三四五六'


def number(value):
    if value.isdigit():return int(value)
    if value=='两':return 2
    return CN.index(value) if len(value)==1 and value in CN else -1


def chain_levels(query):
    """Expand a written range rather than reading just its two endpoints."""
    found=set()
    pattern=rf'({NUM})\s*(?:链)?\s*[-—–~～至到]\s*({NUM})\s*链'
    def span(match):
        lo,hi=map(number,match.groups())
        found.update(range(lo,hi+1) if 0<=lo<=hi<=6 else {-1})
        return ' '
    query=re.sub(pattern,span,query)
    pattern=rf'({NUM})\s*链\s*(?:及其|及)?以上'
    def above(match):
        lo=number(match[1]);found.update(range(lo,7) if 0<=lo<=6 else {-1});return ' '
    query=re.sub(pattern,above,query)
    query=re.sub(rf'(?:至少|不低于)\s*({NUM})\s*链',above,query)
    def below(match):
        hi=number(match[1]);found.update(range(hi+1) if 0<=hi<=6 else {-1});return ' '
    query=re.sub(rf'({NUM})\s*链\s*(?:及其|及)?以下',below,query)
    query=re.sub(rf'(?:至多|不高于)\s*({NUM})\s*链',below,query)
    def less(match):
        hi=number(match[1]);found.update(range(hi) if 1<=hi<=6 else {-1});return ' '
    query=re.sub(rf'(?:低于|不到|不足|未满)\s*({NUM})\s*链',less,query)
    def more(match):
        lo=number(match[1]);found.update(range(lo+1,7) if 0<=lo<6 else {-1});return ' '
    query=re.sub(rf'(?:高于|超过)\s*({NUM})\s*链',more,query)
    found.update(number(m) for m in re.findall(rf'({NUM})\s*链',query))
    if '满链' in query:found.add(6)
    return found


def scope_label(scope):
    lo,hi=scope['min'],scope['max']
    if type(lo)!=int or type(hi)!=int or not 0<=lo<=hi<=6:raise WikiError('主体链数范围无效')
    kind=scope['kind']
    if kind=='exact' and lo==hi:return f'{lo}链'
    if kind=='range':return f'{lo}—{hi}链'
    if kind=='at_least' and hi==6:return f'{lo}链及以上'
    raise WikiError('主体链数范围类型与边界不一致')


def validate_scope(spec,raw):
    labels=spec.get('scope_labels',[])
    if any(label not in ('低链','高链') or label not in raw for label in labels):
        raise WikiError('低/高链标签未在该手法块明示')
    scope=spec.get('owner_chain_scope')
    if not scope:
        if spec['chain_scope']!='原文未标明链数':raise WikiError('原文块不能推断主体链数')
        return
    if spec['chain_scope']!=scope_label(scope):raise WikiError('主体链数显示与范围不一致')
    quote=scope['quote']
    if not quote or quote not in raw or not scope['source_name'] or scope['source_name'] not in quote:
        raise WikiError('主体链数缺少同段原文与主体名称')
    lo,hi=scope['min'],scope['max']
    # Evidence must carry the declared relation, not merely contain the numbers.
    if scope['kind']=='at_least':
        values=[number(n) for n in re.findall(rf'({NUM})\s*链(?:及其|及)?以上',quote)]
        valid=lo in values
    elif scope['kind']=='range':
        pairs=[tuple(map(number,m)) for m in re.findall(rf'({NUM})\s*(?:链)?\s*[-—–~～至到]\s*({NUM})\s*链',quote)]
        valid=(lo,hi) in pairs
    else:valid=lo in chain_levels(quote) and not re.search(r'以上|以下|[-—–~～至到]',quote)
    if not valid:raise WikiError('主体链数关系与原文不一致')


def attach_dependencies(spec,parts):
    """A reuse edge names an exact suffix in an earlier, self-contained block."""
    blocks={b['phase']:b for b in spec['blocks']};positions={b['phase']:i for i,b in enumerate(spec['blocks'])}
    for part in parts:
        refs=blocks[part['phase']].get('depends_on',[])
        if not refs:continue
        if len(refs)!=1:raise WikiError('当前只支持一个明确的后续复用段')
        ref=refs[0];target=blocks.get(ref['target_phase']);reuse=ref['reuse_lines']
        if (ref['relation']!='continues_with_suffix' or not target or not reuse
                or positions[ref['target_phase']]>=positions[part['phase']]):
            raise WikiError('阶段依赖缺失、成环或不是已出现的来源阶段')
        if target.get('depends_on'):raise WikiError('不支持间接复用依赖，须另行核对原文')
        if target['lines'][-len(reuse):]!=reuse:raise WikiError('复用内容不是目标阶段的完整后续原文')
        if not ref['anchor'] or part['raw'].count(ref['anchor'])!=1:raise WikiError('阶段复用缺少唯一的原文引用说明')
        # Require an author-written cross-reference, not just arbitrary overlap.
        if not re.search(r'与.*一致|同.*(?:轮|阶段)|重复.*(?:轮|阶段)',ref['anchor']):
            raise WikiError('阶段复用依据不是原文的阶段引用')
        part['dependencies']=[{'target_phase':ref['target_phase'],'relation':ref['relation'],
                               'anchor':ref['anchor'],'raw':'\n'.join(reuse)}]
    return parts


def attach_action_annotations(spec,parts):
    """Bind an author's inline chain marker to one exact action, never the axis."""
    for block,part in zip(spec['blocks'],parts):
        annotations=[]
        for item in block.get('action_chain_annotations',[]):
            chain=item['chain'];action=item['action'];quote=item['quote']
            if (type(chain)!=int or not 0<=chain<=6 or not item['name'] or not item['target_ids']
                    or not action.startswith(item['name']) or len(action)<=len(item['name'])
                    or not re.fullmatch(re.escape(action)+r'(?:\('+str(chain)+r'链\)|（'+str(chain)+r'链）)',quote)
                    or part['raw'].count(quote)!=1):
                raise WikiError('动作链数标注须为本段唯一、紧邻该动作的完整原文')
            start=part['raw'].index(quote)
            if any(not (start+len(quote)<=a['start'] or start>=a['end']) for a in annotations):
                raise WikiError('动作链数标注重复或重叠')
            annotations.append({**item,'start':start,'end':start+len(quote),'scope':'source_action_only',
                                'applicability':'not_established_by_inline_marker'})
        if annotations:part['action_chain_annotations']=annotations
    return parts
