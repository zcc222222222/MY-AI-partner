"""Source-scoped, reviewed guide annotations and lossless rotation extraction."""
from copy import deepcopy
import hashlib
import json
import re
import sqlite3
from contextlib import closing

from .collector import html_text
from .store import WikiError
from .rotation_conditions import chain_levels, scope_label, validate_scope, attach_dependencies, attach_action_annotations
from .rotation_structure import attach_structure, local_scope_mismatch, validate_paragraph, unexpanded_cycle_request

FORMAT="guide-curation-v1"
OPENING_PHASES={'启动轴','启动','开局攻击轴','配队手法'}
CYCLE_PHASES={'循环轴','循环','②循环招式'}
OPENING_PHASES.update(('清/达双人启动','清/达/莫三人启动'))


def requested_phases(data,query):
    requested={p for p in ('启动轴','循环轴','开局攻击轴') if p in query}
    available={s['phase'] for f in data.get('rotations',[]) for s in f['segments']}
    roles={s.get('phase_role') for f in data.get('rotations',[]) for s in f['segments']}
    role_words={'opening':r'起手|启动|开场|开局','amplification':'增伤','burst':'爆发','cycle':'循环'}
    named_roles={role for role in roles if role in role_words and re.search(role_words[role],query)}
    if available & OPENING_PHASES and '启动' in query and not re.search('冷启动|热启动',query):requested.add('启动轴')
    if available & CYCLE_PHASES and '循环' in query:requested.add('循环轴')
    if 'opening' in roles and re.search('启动|起手|开场|开局',query):requested.add('启动轴')
    if 'cycle' in roles and '循环' in query:requested.add('循环轴')
    if re.search(r'(?:开场|开局)(?:攻击)?(?:手法|流程|轴)',query):requested.add('启动轴')
    if '循环轴' in requested and re.search(r'配队\s*手法',query):requested.add('启动轴')
    if re.search(r'配队\s*手法',query) and any('单人手法' in f['spec']['phases'] for f in data.get('rotations',[])):
        requested.update(('启动轴','循环轴'))
    for flow in data.get('rotations',[]):
        for part in flow['segments']:
            phase=part['phase']
            if (phase==flow['spec'].get('team_variant',{}).get('label')
                    and flow['spec'].get('team_variant',{}).get('members') and len(flow['segments'])>1):
                continue
            if phase not in OPENING_PHASES|CYCLE_PHASES|{'原文流程'} and phase in query:requested.add(phase)
            stage_name={'amplification':'增伤阶段','burst':'爆发阶段'}.get(part.get('phase_role'))
            if stage_name and stage_name in query:requested.add(phase)
            if len(named_roles)>=3 and part.get('phase_role') in named_roles:requested.add(phase)
        for block in flow['spec'].get('blocks',[]):
            if any(cue in query for cue in block.get('selectors',[])):requested.add(block['phase'])
    return requested


def phase_matches(phase,requested,role=None):
    return (not requested or phase in requested or bool(OPENING_PHASES & requested and phase in OPENING_PHASES)
            or bool(CYCLE_PHASES & requested and phase in CYCLE_PHASES)
            or bool(role=='opening' and OPENING_PHASES & requested) or bool(role=='cycle' and CYCLE_PHASES & requested))


def matching_rotation_ids(data,query):
    phases=requested_phases(data,query)
    return {s['claim_id'] for f in matching_flows(data,query) if rotation_scope_matches(f['spec'],query)
            for s in f['segments'] if phase_matches(s['phase'],phases,s.get('phase_role'))}


def matching_flows(data,query):
    flows=data.get('rotations',[])
    member_names={name for f in flows for name in f['spec'].get('team_variant',{}).get('members',[])}
    requested={name for name in member_names if name in query}
    if requested:
        flows=[f for f in flows if requested<=set(f['spec'].get('team_variant',{}).get('members',[]))]
    hits=[(i,m.start(),m.end()) for i,f in enumerate(flows)
          for cue in f['spec'].get('team_variant',{}).get('selectors',[]) for m in re.finditer(re.escape(cue),query)]
    # 清/达 is also a prefix of 清/达/莫. Only a separately occurring label
    # selects the duo when the longer label denotes the trio.
    chosen={i for i,start,end in hits if not any(other!=i and lo<=start and end<=hi and (lo<start or end<hi)
                                                for other,lo,hi in hits)}
    variants=[f for i,f in enumerate(flows) if i in chosen]
    if variants:flows=variants
    selected=[f for f in flows if any(cue in query for cue in f['spec'].get('playstyle',{}).get('selectors',[]))]
    return selected or flows


def at_path(doc,path):
    value=doc
    for part in path:value=value[part]
    if not isinstance(value,str):raise WikiError("标注锚点不是文字字段")
    return value


def source_text(source,path):
    return html_text(at_path(json.loads(source['text']),path))


def fingerprint(source):
    return hashlib.sha256(source['text'].encode()).hexdigest()


def term_claim(term):
    return '作者攻略观点：来源内整理核对，本篇配队与手法中的“'+'／'.join(term['aliases'])+'”对应'+term['name']+'，只适用于本篇上下文，不作为全局角色别名。'


def rotation_claim(spec,part):
    context='；'.join(spec.get('context',[]))
    claim='作者攻略观点：【'+spec['mode']+'】'+spec['chain_scope']+'；'+(context+'；' if context else '')+part['phase']+'（原文完整段落，按原符号保留）：'+part['raw']
    for ref in part.get('dependencies',[]):
        claim+='\n【按原文引用紧接执行的后续段，出处为'+ref['target_phase']+'；不是重新起手】：\n'+ref['raw']
    if part.get('action_chain_annotations'):
        claim+='\n【应用核对】链数括注仅绑定紧邻动作；原文未据此说明整套流程的适用链数，也未提供其他链数的删步或替代方案。'
    if spec.get('coverage_scope')=='local_moves':
        claim+='\n【应用分类】本段是局部招式说明，不能当作完整输出轴。'
    if part.get('branches'):
        claim+='条件分支按各自原文条件分别说明，不把多行接成连续动作，不省略另一种情况。'
    if part.get('cycle_reference'):
        kind=part['cycle_reference']['kind']
        if kind=='partial_ellipsis':claim+='\n【应用核对】原文循环段含省略号，保留已写动作，不补省略内容，不当作完整闭合循环。'
        elif kind=='unspecified_reuse':claim+='\n【应用核对】原文重复引用未明确复用范围或次数，不自行展开，不重跑整套开局。'
        else:claim+='\n【应用核对】原文只给循环起点或顺序，未展开闭合循环；保留引用句，不自行指定复用起点或重跑整套开局。'
    return claim


def rotation_scope_matches(spec,query):
    """Explicit chain requests require explicit source coverage; unknown is not zero."""
    if local_scope_mismatch(spec,query):return False
    if unexpanded_cycle_request(spec,query):return False
    modes=set(re.findall(r'震谐|聚爆',query))
    if modes and spec['mode'] not in modes:return False
    if '进阶' in query and not spec.get('advanced_scope'):return False
    if any(label in query and label not in spec.get('scope_labels',[]) for label in ('低链','高链')):return False
    for condition in spec.get('teammate_chain_conditions',[]):
        number=condition['chain'];name=condition['name']
        numeral='(?:'+str(number)+'|'+'零一二三四五六'[number]+')'
        query=re.sub(r'(?<![0-9零一二三四五六七八九十])'+numeral+r'\s*链\s*'+re.escape(name),name,query)
        query=re.sub(re.escape(name)+r'\s*'+numeral+r'\s*链',name,query)
    # Quoting the source's inline marker is a request about that action, not
    # automatically a request for an axis at the bracketed chain level.
    for block in spec.get('blocks',[]):
        for annotation in block.get('action_chain_annotations',[]):
            query=query.replace(annotation['quote'],annotation['action'])
            query=re.sub(re.escape(annotation['action'])+r'\s+'+str(annotation['chain'])+r'\s*链',annotation['action'],query)
    requested=chain_levels(query)
    if not requested:return True
    owner_scope=spec.get('owner_chain_scope')
    declared=set(range(owner_scope['min'],owner_scope['max']+1)) if owner_scope else chain_levels(spec['chain_scope'])
    return bool(declared) and requested<=declared


def rotation_parts(text,spec):
    lines=text.splitlines()
    if (spec.get('notation') or any(b.get('cycle_reference') for b in spec.get('blocks',[]))) and spec.get('format')!='source-blocks':
        raise WikiError('键位说明及未展开循环须绑定完整原文块')
    if (spec.get('coverage_scope') or any(b.get('phase_role') or b.get('branches') or 'sequence_anchors' in b for b in spec.get('blocks',[]))) and spec.get('format')!='source-blocks':
        raise WikiError('阶段角色和局部分支须绑定完整原文块')
    if (spec.get('team_variant') or spec.get('advanced_scope') or any(b.get('action_chain_annotations') for b in spec.get('blocks',[]))) and spec.get('format')!='source-blocks':
        raise WikiError('动作和队伍变体条件须绑定完整原文块')
    if spec.get('teammate_chain_conditions') and spec.get('format')!='source-blocks':
        raise WikiError("队友链数条件须绑定完整原文块")
    if (spec.get('owner_chain_scope') or spec.get('scope_labels')) and spec.get('format')!='source-blocks':
        raise WikiError('主体链数条件须绑定完整原文块')
    if spec.get('format')=='source-blocks':
        expected_scope=scope_label(spec['owner_chain_scope']) if spec.get('owner_chain_scope') else '原文未标明链数'
        if spec['chain_scope']!=expected_scope or spec['mode']!='原文手法':
            raise WikiError("原文块不能推断模式或链数")
        context=spec.get('context',[])
        if any(not value or lines.count(value)!=1 for value in context):raise WikiError("原文块上下文缺失或不唯一")
        blocks=spec.get('blocks',[])
        if not blocks or spec['phases']!=[b['phase'] for b in blocks] or len(set(spec['phases']))!=len(blocks):
            raise WikiError("原文块阶段缺失、重复或顺序不符")
        parts=[];end=-1
        for block in blocks:
            excerpt=block['lines']
            if not excerpt or any(not isinstance(line,str) or not line or '\n' in line for line in excerpt):
                raise WikiError("原文块必须由完整非空行组成")
            positions=[i for i in range(len(lines)-len(excerpt)+1) if lines[i:i+len(excerpt)]==excerpt]
            if len(positions)!=1 or positions[0]<=end:raise WikiError("原文块缺失、重复、重叠或被重排")
            end=positions[0]+len(excerpt)-1
            raw='\n'.join(excerpt)
            prose=block.get('prose',False)
            if type(prose)!=bool:raise WikiError('文字流程标记须为布尔值')
            if prose:
                labelled=raw.startswith(block['phase']+'：') or (len(excerpt)>1 and excerpt[0].strip('\u200b ')==block['phase'])
                if 'sequence_anchors' in block:
                    validate_paragraph(block,raw)
                elif block['phase']=='原文流程' or not labelled or len(raw)<12:
                    raise WikiError('文字流程须保留原文明确的阶段标题和完整段落')
            elif 'sequence_anchors' in block:raise WikiError('文字顺序锚点不能用于非文字流程')
            elif not re.search('[→↓]',raw):raise WikiError("原文块缺少流程连接符")
            if block['phase']!='原文流程' and block['phase'] not in raw:raise WikiError("阶段名称未在原文块明示")
            if any(not cue or cue not in raw for cue in block.get('selectors',[])):raise WikiError("流程选择条件未在原文块明示")
            parts.append({'phase':block['phase'],'raw':raw,'steps':re.split('[→↓]',raw),
                          'notation_gap':bool(re.search('[→↓]\\s*[→↓]',raw))})
            if prose:parts[-1]['step_format']='source_paragraph' if 'sequence_anchors' in block else 'source_prose'
        for condition in spec.get('teammate_chain_conditions',[]):
            chain=condition['chain']
            if (type(chain)!=int or not 0<=chain<=6 or not condition['name'] or not condition['target_ids']
                    or condition['text']!=str(chain)+'链'+condition['name']
                    or not any(condition['text'] in p['raw'] for p in parts)):
                raise WikiError("队友链数条件未在原文块明示")
        validate_scope(spec,'\n'.join(p['raw'] for p in parts))
        style=spec.get('playstyle')
        if style:
            quote=style['quote'];available='\n'.join(context+[p['raw'] for p in parts])
            if (not quote or quote not in available or not style['label'] or style['label'] not in quote
                    or not style['selectors'] or any(not cue or cue not in quote for cue in style['selectors'])):
                raise WikiError('玩法选择缺少本流程的原文依据')
        variant=spec.get('team_variant')
        if variant:
            startup='\n'.join(p['raw'] for p in parts if p['phase'] in OPENING_PHASES)
            members=variant.get('members')
            if members is not None:
                if (not isinstance(members,list) or len(members)<2 or any(not isinstance(n,str) or len(n)<2 for n in members)
                        or len(set(members))!=len(members) or any(n not in variant['quote'] for n in members)
                        or variant['quote']!='【'+' & '.join(members)+'】'
                        or not any(p['raw'].splitlines()[0]==variant['quote'] for p in parts)):
                    raise WikiError('队伍成员须由本流程首行的完整原文标题支持')
                startup='\n'.join(p['raw'] for p in parts)
            if (not variant['quote'] or variant['quote'] not in startup or not variant['label'] or variant['label'] not in variant['quote']
                    or not variant['selectors'] or any(not cue or cue not in variant['quote'] for cue in variant['selectors'])):
                raise WikiError('队伍变体须由本流程起手中的原文标签支持')
        advanced=spec.get('advanced_scope')
        if advanced and (advanced not in context or not re.search('进阶.*轴',advanced)):
            raise WikiError('进阶标记须保留原文明确上下文')
        return attach_structure(spec,attach_action_annotations(spec,attach_dependencies(spec,parts)))
    if spec.get('format')=='exact-lines':
        # This format deliberately has no inferred chain or combat mode.
        if spec['chain_scope']!='原文未标明链数' or spec['mode']!='原文配队':
            raise WikiError("逐行输出轴不能推断模式或链数")
        if not spec.get('context') or any(lines.count(line)!=1 for line in spec['context']):
            raise WikiError("输出轴键位或条件上下文缺失或不唯一")
        parts=spec['lines']
        if not parts or spec['phases']!=[p['phase'] for p in parts]:raise WikiError("输出轴阶段与原文行不一致")
        positions=[]
        for p in parts:
            if lines.count(p['line'])!=1 or not p['line'].startswith(p['phase']+'：') or '→' not in p['line']:
                raise WikiError("输出轴原文行缺失、重复或阶段不符")
            positions.append(lines.index(p['line']))
        if positions!=list(range(positions[0],positions[0]+len(parts))):raise WikiError("输出轴原文阶段不相邻或被重排")
        if any(lines.index(line)>=positions[0] for line in spec['context']):raise WikiError("输出轴上下文不在阶段之前")
        return [{'phase':p['phase'],'raw':p['line'],'steps':p['line'].split('→'),'notation_gap':'→→' in p['line']} for p in parts]
    if spec['heading'] not in lines:raise WikiError("原文轴标题已变化")
    if spec['mode'] not in spec['heading'] or spec['chain_scope'] not in spec['heading']:
        raise WikiError("模式或链数条件未在该轴标题中明示")
    heading=lines.index(spec['heading'])
    indexes=[i for i,line in enumerate(lines) if line.startswith(spec['prefix'])]
    if len(indexes)!=1 or indexes[0]<heading:raise WikiError("输出轴段落不是唯一的标题内段落")
    if any(line.startswith('★') for line in lines[heading+1:indexes[0]]):raise WikiError("输出轴与适用条件不在同一小节")
    raw=lines[indexes[0]][len(spec['prefix']):]
    if spec['phases']==['启动轴','循环轴']:
        match=re.fullmatch(r'(.*?)\(启动轴\)[；;](.*?)\(循环轴\)[；;]?',raw)
        if not match:raise WikiError("未完整识别启动轴与循环轴，未截断原文")
        values=list(match.groups())
    elif spec['phases']==['进阶轴']:
        values=[raw]
    else:raise WikiError("不支持的输出轴阶段")
    if any(not value.strip() or '→' not in value for value in values):raise WikiError("输出轴步骤缺失")
    return [{'phase':phase,'raw':value,'steps':value.split('→'),'notation_gap':'→→' in value}
            for phase,value in zip(spec['phases'],values)]


def validate_curation(source,page):
    data=page.get('guide_curation')
    if not data:return
    if (data.get('schema')!=FORMAT or data.get('source_id')!=source['source_id']
            or data.get('source_revision')!=source['revision'] or data.get('source_sha256')!=fingerprint(source)):
        raise WikiError("缩写/输出轴标注与来源修订不一致")
    text=source_text(source,data['source_path'])
    if data.get('coverage_review'):
        from .guide_coverage_review import validate_review
        validate_review(source,data['coverage_review'])
    claims={c['claim_id']:c for c in page['claims']}
    expected_extra=[]
    for path in [data['source_path'],*[f['spec'].get('source_path',data['source_path']) for f in data['rotations']]]:
        if path[-1]=='skillDisplayDescription' and path not in expected_extra:expected_extra.append(path)
    if data.get('extra_source_paths',[])!=expected_extra:raise WikiError('补充证据未与手法字段一致绑定')
    for term in data['terms']:
        if term['name'] not in text or not term['target_ids'] or not term['aliases'] or not term['support']:
            raise WikiError("局部缩写缺少全名与核对证据")
        if any(s not in text for s in term['support']) or any(a not in text for a in term['aliases']):
            raise WikiError("局部缩写证据不在锁定的原文字段")
        if claims.get(term['claim_id'],{}).get('text')!=term['claim_text'] or term['claim_text']!=term_claim(term):
            raise WikiError("局部缩写标注与审核结论不一致")
    for flow in data['rotations']:
        owner=flow['spec'].get('owner_chain_scope')
        document=json.loads(source['text'])
        if owner and (str(owner['owner_id'])!=str(document.get('role_id',''))
                      or owner['name'] not in document['title'] or owner['source_name'] not in owner['name']):
            raise WikiError('主体链数条件与攻略主体不一致')
        if any(str(json.loads(source['text']).get('role_id','')) in map(str,c['target_ids']) for c in flow['spec'].get('teammate_chain_conditions',[])):
            raise WikiError("不能把攻略主体的链数当作队友链数排除")
        parts=rotation_parts(source_text(source,flow['spec'].get('source_path',data['source_path'])),flow['spec'])
        if len(parts)!=len(flow['segments']):raise WikiError("输出轴阶段缺失")
        for actual,segment in zip(parts,flow['segments']):
            if actual!={k:v for k,v in segment.items() if k not in ('claim_id','claim_text')}:raise WikiError("输出轴被重排、截断或补写")
            if claims.get(segment['claim_id'],{}).get('text')!=segment['claim_text'] or segment['claim_text']!=rotation_claim(flow['spec'],actual):
                raise WikiError("完整输出轴与审核结论不一致")
            from .payload import citations_for
            if any(c.get('source_path')!=flow['spec'].get('source_path',data['source_path']) for c in citations_for(page,claims[segment['claim_id']])):
                raise WikiError('手法结论引用了其他文字字段')


def validate_targets(terms,knowledge_path):
    with closing(sqlite3.connect(knowledge_path.resolve().as_uri()+'?mode=ro',uri=True)) as conn:
        for term in terms:
            for eid in term['target_ids']:
                row=conn.execute('SELECT name FROM characters WHERE CAST(id AS TEXT)=?',(str(eid),)).fetchone()
                if not row or row[0]!=term['name']:raise WikiError("局部缩写角色ID与已核对全名不一致")


def build_page(source,page,manifest,knowledge_path):
    if (manifest['schema']!=FORMAT or source['source_id']!=manifest['source_id']
            or source['revision']!=manifest['source_revision'] or fingerprint(source)!=manifest['source_sha256']):
        raise WikiError("标注清单已过期，须重新核对原文")
    if page.get('document_metadata',{}).get('source_id')!=source['source_id']:
        raise WikiError("不能将局部标注应用到另一篇攻略")
    owners=[{'name':spec['owner_chain_scope']['name'],'target_ids':[spec['owner_chain_scope']['owner_id']]}
            for spec in manifest['rotations'] if spec.get('owner_chain_scope')]
    actions=[a for spec in manifest['rotations'] for b in spec.get('blocks',[]) for a in b.get('action_chain_annotations',[])]
    validate_targets(manifest['terms']+owners+[c for spec in manifest['rotations'] for c in spec.get('teammate_chain_conditions',[])]+actions,knowledge_path)
    from .documents import evidence
    extra=[]
    for path in [manifest['source_path'],*[spec.get('source_path',manifest['source_path']) for spec in manifest['rotations']]]:
        if path[-1]=='skillDisplayDescription' and path not in extra:extra.append(list(path))
    evidence_page=deepcopy(page)
    evidence_page['guide_curation']={'extra_source_paths':extra}
    entries=evidence(source,evidence_page)
    matching=[(i,e) for i,e in enumerate(entries,1) if e['source_path']==manifest['source_path']]
    if len(matching)!=1:raise WikiError("标注字段不属于本篇已投影章节")
    i,entry=matching[0]
    text=source_text(source,manifest['source_path'])
    result=deepcopy(page)
    result.pop('base_revision',None)
    old_ids={c['claim_id'] for c in result['claims'] if c['claim_id'].startswith('curation-')}
    if old_ids:
        # Reapply only to the exact previous curation, never delete normal claims.
        if not result.get('guide_curation'):raise WikiError("保留ID被其他结论占用")
        result['claims']=[c for c in result['claims'] if c['claim_id'] not in old_ids]
    if result.get('guide_curation'):
        result['body']=result['body'].split('\n## 来源内人工核对补充\n')[0].rstrip()+'\n'
    data={k:deepcopy(manifest[k]) for k in ('schema','source_id','source_revision','source_sha256','source_path','terms','unresolved_terms')}
    data.update(manifest_sha256=hashlib.sha256(json.dumps(manifest,ensure_ascii=False,sort_keys=True).encode()).hexdigest(),rotations=[])
    if extra:data['extra_source_paths']=extra
    if manifest.get('coverage_review'):data['coverage_review']=deepcopy(manifest['coverage_review'])
    body=['','## 来源内人工核对补充','']
    def add(cid,heading,claim,evidence_index=i,evidence_entry=entry):
        result['claims'].append({'claim_id':cid,'text':claim,'evidence_ids':[f'E{evidence_index:03d}'],
                                'valid_periods':[],'citations':deepcopy(evidence_entry['citations'])})
        body.extend(['## '+heading,'',f"- {claim} [source:{source['source_id']}@{source['revision']}]",''])
    for n,term in enumerate(data['terms']):
        term['claim_id']=f'curation-term-{n}'
        term['claim_text']=term_claim(term)
        add(term['claim_id'],'来源内缩写核对',term['claim_text'])
    for spec in manifest['rotations']:
        flow={'spec':deepcopy(spec),'segments':[]}
        path=spec.get('source_path',manifest['source_path'])
        anchors=[(n,e) for n,e in enumerate(entries,1) if e['source_path']==path]
        if len(anchors)!=1:raise WikiError('手法字段不属于本页或明确补充证据')
        n,anchor=anchors[0]
        for part in rotation_parts(source_text(source,path),spec):
            cid='curation-rotation-'+spec['id']+'-'+str(len(flow['segments']))
            claim=rotation_claim(spec,part)
            add(cid,spec['mode']+'输出轴／'+part['phase'],claim,n,anchor)
            flow['segments'].append({**part,'claim_id':cid,'claim_text':claim})
        data['rotations'].append(flow)
    result['body']+='\n'.join(body)
    result['guide_curation']=data
    validate_curation(source,result)
    return result


def response_context(page,matches,query=''):
    data=page.get('guide_curation')
    if not data:return {}
    text='\n'.join(m['text'] for m in matches)
    ids={m['claim_id'] for m in matches}
    terms=[{'aliases':t['aliases'],'name':t['name'],'target_ids':t['target_ids']} for t in data['terms'] if any(a in text for a in t['aliases'])]
    unresolved=[a for a in data['unresolved_terms'] if a in text or a in query]
    rotations=[]
    for flow in data['rotations']:
        for s in flow['segments']:
            if s['claim_id'] not in ids:continue
            info={'claim_id':s['claim_id'],'mode':flow['spec']['mode'],'chain_scope':flow['spec']['chain_scope'],
                  'phase':s['phase'],'source_segment_complete':True,'notation_gap':s['notation_gap']}
            if flow['spec'].get('source_path'):info['source_path']=list(flow['spec']['source_path'])
            if flow['spec'].get('owner_chain_scope'):info['owner_chain_scope']=deepcopy(flow['spec']['owner_chain_scope'])
            if flow['spec'].get('playstyle'):info['playstyle']=flow['spec']['playstyle']['label']
            if flow['spec'].get('team_variant'):info['team_variant']=flow['spec']['team_variant']['label']
            if flow['spec'].get('team_variant',{}).get('members'):info['team_members']=list(flow['spec']['team_variant']['members'])
            if flow['spec'].get('advanced_scope'):info['advanced_scope']=flow['spec']['advanced_scope']
            if flow['spec'].get('coverage_scope'):info['coverage_scope']=flow['spec']['coverage_scope']
            if s.get('phase_role'):info['phase_role']=s['phase_role']
            if flow['spec'].get('notation'):info['notation_case_sensitive']=True
            if s.get('cycle_reference'):info['cycle_reference']=deepcopy(s['cycle_reference'])
            if s.get('step_format')=='source_paragraph':info['step_format']=s['step_format']
            if s.get('branches'):info['branches']=deepcopy(s['branches'])
            if s.get('action_chain_annotations'):info['action_chain_annotations']=deepcopy(s['action_chain_annotations'])
            if s.get('dependencies'):
                info['dependencies']=[{'target_phase':r['target_phase'],'relation':r['relation'],'anchor':r['anchor'],
                                       'source_excerpt_complete':True,'included_in_claim':True} for r in s['dependencies']]
            rotations.append(info)
    context={'schema':FORMAT,'curation_origin':'application_source_review','scope':'this_source_revision_only','terms':terms,'unresolved_terms':unresolved,'rotation_segments':rotations,
            'note':'缩写映射由应用核对该来源上下文生成，不表示作者另行提供了缩写表，也不是全局别名。“原文未标明链数”是应用的核对结论，不能说作者原话写了这句话。mode中的“原文手法／原文配队”和phase中的“原文流程”是应用分类标签，不是作者标注的标题；不要说原文标着这些名称。完整仅指保留原文该段，不表示实战验证；A/E/Q/R等符号原样保留，连续箭头不补动作。'}
    context['coverage']={'annotated_segments':sum(len(f['segments']) for f in data['rotations']),
                         'delivered_segments':len(rotations),'complete_source_document':False}
    if data.get('coverage_review'):
        from .guide_coverage_review import public_review
        context['coverage_review']=public_review(data['coverage_review'])
        context['quality_review_status']=page.get('guide_quality',{}).get('status','no_hold_recorded')
        context['note']+='coverage_review是文字覆盖审核；reviewed_text不等于质量待核对。quality_review_status=no_hold_recorded时不能宣称本篇被标为待核对、不可引用；未做游戏实测和已有质量挂起是不同情况。'
        if data['coverage_review']['status']!='reviewed_text':
            context['uncovered_rotation_request']=data['coverage_review']['note']+' 已有配装等摘录不能代替完整动作轴；只说明已审核文字的覆盖限制，不断言原站或游戏没有。'
    if any(s.get('source_path') for s in rotations):
        context['note']+='source_path标明动作所在字段，基础连招文字与正文/摘要是不同原文方案，分别引用，不串成一轮，不推断谁取代谁或强行统一动作次数；文字面板补充不代表识别了图片或视频。'
    if any(r.get('dependencies') for r in rotations):
        context['note']+='依赖后续段已附在对应结论内，接在循环段之后执行；“出处为启动”不表示重新执行整套启动，也不表示返回了完整启动阶段。'
    context['note']+='链数核对只涉及对应手法块，不表示整篇其他字段没提过链数。返回一段不表示来源只有一段，不能断言原文唯一或仅有此手法。'
    conditions=[{**c,'claim_id':s['claim_id'],'scope':'this_team_segment_only'}
                for f in data['rotations'] for s in f['segments'] if s['claim_id'] in ids
                for c in f['spec'].get('teammate_chain_conditions',[]) if c['text'] in s['raw']]
    if conditions:context['teammate_chain_conditions']=conditions
    scoped_flows=[f for f in matching_flows(data,query) if rotation_scope_matches(f['spec'],query)]
    scope_match=bool(scoped_flows)
    cycle_refs=[{**deepcopy(s['cycle_reference']),'phase':s['phase'],'included_in_claim':s['claim_id'] in ids}
                for f in matching_flows(data,query) for s in f['segments'] if s.get('cycle_reference')]
    if cycle_refs:
        context['cycle_references']=cycle_refs
        context['note']+='cycle_references保留作者的循环引用或省略提示，expanded=false表示未展开完整循环；保留已写动作，不补省略或复用范围，不重跑全部开局，不删除等待条件。引用目录不表示本次返回了完整动作段。'
    if any(unexpanded_cycle_request(f['spec'],query) for f in matching_flows(data,query)):
        context['unexpanded_cycle_request']='本篇已整理段含循环起点、顺序、复用或省略提示，尚未据此核对展开完整循环轴；不能补动作，也不能说原文没有循环提示。'
    if any(r.get('notation_case_sensitive') for r in rotations):
        context['note']+='本段原文分别定义小写e与大写E，必须保留大小写，不统一换写；其他符号未定义的保留原样，不因a的定义就额外给A造释义。短横线简写和后面的文字是同一方案，不串成两轮。'
    if rotations and any(r.get('team_members') for r in rotations):
        context['note']+='team_members来自本段完整原文队伍标题；按本段成员说明，不把多支队伍的动作拼接、不用未返回的队伍替换。动作中的短称和符号保留原样。'
    elif rotations and any(r.get('team_variant') for r in rotations):
        context['note']+='队伍标签保持原文缩写，不推断全名。相同循环原文在不同队伍下的关联是应用按同一小节整理，不表示更换任意队友仍等效；这里是攒回路招式，不冒充完整队伍实战轴。'
    action_notes=[{**deepcopy(a),'claim_id':s['claim_id']}
                  for f in matching_flows(data,query) for s in f['segments']
                  if s['claim_id'] in ids or not scope_match or not rotations
                  for a in s.get('action_chain_annotations',[])]
    if action_notes:
        context['action_chain_annotations']=action_notes
        context['note']+='action_chain_annotations是单步原文括注，不是owner_chain_scope或整队门槛；不将4链解释为至少4链，不据此断言零链不能释放该技能，不删除该步或自造替代手法。指定链数未覆盖时只能引用该短标注说明未知，不能把整个未知链数流程当匹配答案。'
    scoped_parts=[s for f in scoped_flows for s in f['segments']]
    missing=[p for p in sorted(requested_phases(data,query)) if not any(phase_matches(s['phase'],{p},s.get('phase_role')) for s in scoped_parts)]
    if any(r.get('coverage_scope')=='local_moves' for r in rotations):
        context['note']+='coverage_scope=local_moves表示局部招式，不能冒充完整轮转；branches各行按原条件分别对应结果，不把被攻击/未被攻击串行执行，不自行推断触发时机或斜杠符号的一一对应。'
    if any(r.get('phase_role') for r in rotations):
        context['note']+='phase_role是按原文阶段名核对的应用分类，展示时保留原文名称，不把起手和充能阶段当成整套队伍流程。'
    if any(r.get('step_format')=='source_paragraph' for r in rotations):
        context['note']+='无独立标题的文字流程用应用标签“原文流程”，不说作者写了该标题；顺序锚点只供原文校验，不是额外动作。站场与变奏入场省略条件按原文分别解释，不拼成两轮，不把个人满协奏流程改称全队循环。'
        context['note']+='展示可称“这段流程”，不能说“作者只标了原文流程”；claim前缀的应用标签、链数核对结论不要放进作者原文引号中。'
        context['note']+='括注用途只表示作者在此处的操作理由，不能反推技能没有伤害或没有其他用途，也不能把该方案前提推广为所有玩法都必须如此。'
        context['note']+='次数和括号数值只按原文整组引用，不解释为每次获得多少，不计算总量、累计值或下一轮数值。'
        if '首先队友获得同奏后，我们切心月狐同奏入场' in text:
            context['note']+='本段顺序是“队友获得同奏后，我们切心月狐同奏入场”，开一段大后“切队友下场”，然后“队友再切心月狐变奏入场后”。第一次同奏入场前不能凭空再加一次“先切队友”；切人对象和两种入场不能颠倒。'
        if '2次空中攻击（50核心）' in text:
            context['note']+='本段两处“2次空中攻击（50核心）”各是一整组原文括注，不能由它推出每次空中攻击50核心；不据此推成100，也不判断50是新增量还是已有累计量。只保留两处原文，不做数值换算。'
    if any(local_scope_mismatch(f['spec'],query) for f in matching_flows(data,query)) and not rotations:
        context['uncovered_local_rotation_request']='已有标注仅为局部招式或条件分支，不是本次要求的完整轮转；不能据此说原站没有完整轴。'
    missing_phase=bool(data['rotations']) and scope_match and bool(missing)
    if data['rotations'] and (not scope_match or missing_phase):
        if not matching_flows(data,query) and any(f['spec'].get('team_variant',{}).get('members') for f in data['rotations']):
            context['uncovered_team_request']='本次点名的成员组合没有匹配已整理的原文队伍，不得从不同队伍拼动作或自行换队友。'
        if missing_phase:
            context['uncovered_phase_request']='本次指定阶段没有匹配的完整原文块：'+'、'.join(missing)+'；原文流程、个人连段、基础手法不能改称作者未提供的启动轴或循环轴。'
        else:
            context['uncovered_rotation_request']='本篇完整整理段落的模式与链数范围：'+'、'.join(sorted({f['spec']['mode']+'／'+f['spec']['chain_scope'] for f in data['rotations']}))+'。本次要求未匹配已整理的完整段落，不得把未注明模式、链数或基础手法改称指定模式、链数或进阶的完整轴。'
        context['available_rotation_scopes']=[{'mode':f['spec']['mode'],'chain_scope':f['spec']['chain_scope'],
                                             'phases':[s['phase'] for s in f['segments']],
                                             **({'playstyle':f['spec']['playstyle']['label']} if f['spec'].get('playstyle') else {})} for f in data['rotations']]
        key='uncovered_phase_request' if missing_phase else 'uncovered_rotation_request'
        context[key]+='已有阶段见available_rotation_scopes，仅是范围目录，不表示本次返回这些阶段动作；不能说原文没有这些阶段，也不能从未标链数推断不适用于零链。'
    return context
