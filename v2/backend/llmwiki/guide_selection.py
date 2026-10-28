"""Topic coverage and recommendation context for bounded guide excerpts, never new facts."""
import re
from .search import rank

TOPICS={
    "weapon": (r"武器|专武|迅刀|长刃|佩枪|臂铠|音感仪", r"武器|配装", ("weaponTexts","weapon")),
    "echo": (r"声骸|词条|套装|合鸣|cost|暴击|共鸣效率", r"声骸|词条|套装|合鸣", ("echoTexts","echo")),
    "team": (r"配队|队友|队伍|阵容|组队|搭配谁|带谁", r"配队|队伍|阵容|编队|体系选择|战斗环境", ("teammate",)),
    "rotation": (r"手法|输出轴|轮椅轴|启动|起手|开场|开局.*轴|操作|循环|连招|流程|个人连段|格挡|反击|招式衔接|增伤阶段|爆发阶段", r"手法|输出轴|轮椅轴|操作|循环|连招|流程|启动|连段", ()),
}


def select_claims(page,eligible,headings,query,query_terms):
    from .payload import citations_for
    from .guide_curation import rotation_scope_matches, matching_rotation_ids
    from .guide_quality import is_held
    if is_held(page):
        return [c for c in eligible if c.get('evidence_ids')==['E001']],['quality_hold']
    curated=page.get('guide_curation',{})
    if (curated and re.search(r'缩写|简称|全名|对应谁|指谁',query)
            and not re.search(TOPICS['rotation'][0],query,re.I)):
        ids={t['claim_id'] for t in curated['terms']}
        matches=[c for c in eligible if c['claim_id'] in ids]
        return sorted(matches,key=lambda c:-rank(c['text'],query_terms)),['source_terms']
    eligible=[c for c in eligible if not c['claim_id'].startswith('curation-term-')]
    allowed=matching_rotation_ids(curated,query)
    # Apply scope before topic pooling: a rotation claim can also match a team
    # heading, and must not re-enter through that second topic.
    eligible=[c for c in eligible if not c['claim_id'].startswith('curation-rotation-') or c['claim_id'] in allowed]
    topics=[key for key,(pattern,_,_) in TOPICS.items() if re.search(pattern,query,re.I)]
    coverage_blocked=(curated.get('coverage_review',{}).get('status') in ('no_action_sequence','ambiguous_chain_boundary') and 'rotation' in topics)
    metadata_only=[c for c in eligible if c.get('evidence_ids')==['E001']]
    if coverage_blocked:
        if set(topics)<={'rotation','team'}:return metadata_only,['rotation_uncovered']
        paths=curated['coverage_review']['reviewed_paths']
        eligible=[c for c in eligible if c in metadata_only or not any(cite.get('source_path') in paths for cite in citations_for(page,c))]
        topics=[t for t in topics if t!='rotation']
    if 'rotation' not in topics and any(s['phase'] in query for f in curated.get('rotations',[]) for s in f['segments']):
        topics.append('rotation')
    if 'echo' in topics and re.search(r"(?:带|有|无|没|没有)专武",query) and '武器' not in query:
        topics=[t for t in topics if t!='weapon']
    if not topics:
        if re.search(r"攻略|养成|培养|配装",query): topics=["weapon","echo","team"]
        else:
            for key in ('weapon','echo'):
                fields=TOPICS[key][2]
                if any(rank(re.sub(r'^作者攻略观点：','',c['text']),query_terms) and any(any(field in cite.get('source_path',[]) for field in fields) for cite in citations_for(page,c)) for c in eligible):
                    topics.append(key)
            if not topics:return None
    if (curated.get('rotations') and 'rotation' in topics and set(topics)<={'rotation','team'}
            and not allowed):
        # A known source with an unsupported scope is useful evidence, but its
        # old mixed-phase summary must not substitute for the requested axis.
        return [c for c in eligible if c.get('evidence_ids')==['E001']],['rotation_uncovered']
    pools={}
    modes=set(re.findall(r"震谐|聚爆",query))
    for topic in topics:
        _,heading_pattern,fields=TOPICS[topic]
        def belongs(claim):
            paths=[cite.get('source_path',[]) for cite in citations_for(page,claim)]
            if any(any(field in path for field in fields) for path in paths):return True
            other_fields=[field for key,(_,_,fs) in TOPICS.items() if key!=topic for field in fs]
            if paths and all(any(field in path for field in other_fields) for path in paths):return False
            heading=headings.get(claim['claim_id'],'')
            if not re.search(heading_pattern,heading):return False
            heading_topics={key for key,(_,pattern,_) in TOPICS.items() if re.search(pattern,heading)}
            if len(heading_topics)>1 and not re.search(TOPICS[topic][0],claim['text'],re.I):
                if any(re.search(TOPICS[key][0],claim['text'],re.I) for key in heading_topics-{topic}):return False
            return True
        pool=[c for c in eligible if belongs(c)]
        other_rotation_scope=bool(curated.get('rotations')) and not allowed
        if topic=='rotation':
            pool=[c for c in pool if not c['claim_id'].startswith('curation-rotation-') or c['claim_id'] in allowed]
        if not pool: continue
        if topic in ('team','rotation') and len(modes)==1:
            pool=[c for c in pool if not (set(re.findall(r"震谐|聚爆",c['text']))-modes) or bool(modes & set(re.findall(r"震谐|聚爆",c['text'])))]
        def priority(claim):
            text=claim['text']
            # Preserve explicit ordering and prerequisites before lengthy effects.
            summary=2 if re.search(r"主推|首选|下位|替代|不推荐|不能|必须|仅在|前提",text) else 1 if re.search(r"推荐|优先|次选|选择|需要|没有|无专武",text) else 0
            return (-(summary*6+rank(text,query_terms)),claim['claim_id'])
        ordered=sorted(pool,key=priority)
        if topic=='rotation' and curated and not other_rotation_scope and '合轴点' not in query:
            preferred=[]
            for flow in curated['rotations']:
                if modes and flow['spec']['mode'] not in modes:continue
                preferred += [s['claim_id'] for s in flow['segments'] if s['claim_id'] in allowed]
            exact=[c for c in pool if c['claim_id'] in preferred]
            if exact:
                # Full segments already include source conditions/notation.
                # Older summaries may combine phases and repeat the opening.
                ordered=exact
        if topic=='team' and ordered:
            # Abbreviated lineups need the adjacent explanation from the same
            # source field. Unrelated UI alternatives are not that explanation.
            seeds=ordered[:2]
            positions={c['claim_id']:i for i,c in enumerate(eligible)}
            context=[]
            for seed in seeds:
                if not re.search(r"首选.*[、/]",seed['text']):continue
                anchors={repr(cite.get('source_path')) for cite in citations_for(page,seed)}
                for candidate in pool:
                    if (candidate not in seeds and 0<positions[candidate['claim_id']]-positions[seed['claim_id']]<=3
                            and headings.get(candidate['claim_id'])==headings.get(seed['claim_id'])
                            and not re.search(r"首选|次选|界面",candidate['text'])
                            and anchors & {repr(cite.get('source_path')) for cite in citations_for(page,candidate)}):
                        if candidate not in context:context.append(candidate)
            ordered=seeds+context+[c for c in ordered if c not in seeds and c not in context]
        pools[topic]=ordered
    if not pools:
        if coverage_blocked:return metadata_only,['rotation_uncovered']
        if curated.get('rotations') and 'rotation' in topics and not any(rotation_scope_matches(f['spec'],query) for f in curated['rotations']):
            return [],['rotation_uncovered']
        return None
    selected=[];seen=set()
    # First cover every requested topic; then fill each topic without duplicates.
    for offset in range(max(map(len,pools.values()))):
        for topic in topics:
            pool=pools.get(topic,[])
            if offset<len(pool) and pool[offset]['claim_id'] not in seen:
                selected.append(pool[offset]);seen.add(pool[offset]['claim_id'])
    selected_roles={s.get('phase_role') for f in curated.get('rotations',[]) for s in f['segments'] if s['claim_id'] in allowed}-{None}
    if 'rotation' in topics and (re.search(r'完整.{0,8}(?:手法|流程|输出轴)',query) or len(selected_roles)>=3):
        # A full flow must survive the five-claim cap before incidental gear
        # keywords consume its middle or final stage.
        selected.sort(key=lambda c:not c['claim_id'].startswith('curation-rotation-'))
    return selected,[topic for topic,pool in pools.items() if pool]
