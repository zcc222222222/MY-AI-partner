"""Bounded evidence excerpts, independent of any chat prompt or vector service."""
import json
import re
from .payload import citations_for


def historical_requested(query):
    for match in re.finditer(r'历史|旧版|旧稿|旧攻略|过往',query):
        before=query[max(0,match.start()-16):match.start()]
        after=query[match.end():match.end()+16]
        if (re.search(r'(?:不要|不用|不看|不查|别|排除|不采用|无需)(?:再)?(?:使用|用|看|查|参考)?(?:任何)?\s*$',before)
                or re.match(r'(?:资料|攻略)?(?:不能|不要|不用|不可|不再)(?:使用|用|拿|作为|来|顶替)',after)):
            continue
        return True
    return False


def excerpt_snippet(matches):
    # Curated rotations already retain full, atomic text in matches. Repeating
    # it in snippet can evict a different team's complete evidence block.
    if matches and all(c['claim_id'].startswith('curation-rotation-') for c in matches):
        return '\n'.join(c['heading'] for c in matches)+'（完整原文见matches）'
    return '\n'.join(('【'+c['heading']+'】' if c['heading'] else '')+c['text'] for c in matches)


def terms(query):
    parts = re.findall(r"[\u3400-\u9fff]+|[a-zA-Z0-9]+", query.lower())
    result = {query.lower()}
    for part in parts:
        if len(part) <= 2:
            result.add(part)
        else:
            result.update(part[i:i + n] for n in (2, 3) for i in range(len(part) - n + 1))
    return sorted(result, key=lambda t: (-len(t), t))[:64]


def rank(text, query_terms):
    text = text.lower()
    return sum(len(t) ** 2 for t in query_terms if t in text)


def headings_for(page):
    result, heading = {}, ""
    body=page['body'];offset=0
    for full_line in body.splitlines(keepends=True):
        line=full_line.rstrip('\r\n')
        if line.startswith("## "):
            heading = line[3:].strip()
        elif line.startswith("- "):
            for claim in page["claims"]:
                if body.startswith(claim['text'],offset+2):
                    result[claim["claim_id"]] = heading
        offset+=len(full_line)
    return result


def search(store, query, *, game_version, locale="zh", limit=5, max_chars=8000, entities=None, kind=None, reviewed_only=False, document_filters=None):
    from .guide_quality import is_held
    q = str(query).strip()[:200]
    if not q:
        return []
    store.refresh(game_version=game_version, locale=locale)
    query_terms = terms(q)
    limit = max(1, min(int(limit), 20))
    with store.connection() as conn:
        filters, filter_params = "", []
        if document_filters is not None:
            filters += " AND json_type(r.payload,'$.document_metadata')='object'"
            history=document_filters.get('include_historical')
            if not document_filters.get('applicable_game_version') and not (history is True or history is None and historical_requested(q)):
                filters+=" AND COALESCE(json_extract(r.payload,'$.document_metadata.source_role'),'primary')!='historical_supplement'"
            for key in ("category", "applicable_game_version", "author"):
                if document_filters.get(key):
                    filters += f" AND json_extract(r.payload,'$.document_metadata.{key}')=?"
                    filter_params.append(document_filters[key])
            if document_filters.get("updated_on"):
                filters += " AND substr(json_extract(r.payload,'$.document_metadata.source_date'),1,10)=?"
                filter_params.append(document_filters["updated_on"])
            if document_filters.get("entity"):
                filters += " AND EXISTS(SELECT 1 FROM json_each(r.payload,'$.document_metadata.entity_ids') e WHERE e.value=?)"
                filter_params.append(document_filters["entity"])
            if document_filters.get("entities"):
                values = document_filters["entities"]
                filters += " AND EXISTS(SELECT 1 FROM json_each(r.payload,'$.document_metadata.entity_ids') e WHERE e.value IN (" + ",".join("?" for _ in values) + "))"
                filter_params.extend(values)
        if entities:
            selected_entities = sorted(set(entities))[:20]
            placeholders = ",".join("?" for _ in selected_entities)
            filters += " AND EXISTS (SELECT 1 FROM page_tags t WHERE t.page_id=p.page_id AND t.game_version=p.game_version AND t.locale=p.locale AND t.tag_type='entities' AND t.value IN (" + placeholders + "))"
            filter_params.extend(selected_entities)
        if kind:
            filters += " AND COALESCE(json_extract(r.payload,'$.kind'),'reference')=?"
            filter_params.append(kind)
        if reviewed_only:
            filters += (" AND EXISTS (SELECT 1 FROM drafts d JOIN reviews v ON v.draft_id=d.draft_id AND v.fingerprint=d.fingerprint "
                        "WHERE d.page_id=p.page_id AND d.game_version=p.game_version AND d.locale=p.locale "
                        "AND d.fingerprint=r.fingerprint AND d.state='published' "
                        "AND v.review_id=(SELECT MAX(review_id) FROM reviews WHERE draft_id=d.draft_id) AND v.decision='approved')")
        # FTS selects candidates; deterministic claim ranking selects bounded text.
        use_fts = conn.execute("SELECT value FROM meta WHERE key='fts'").fetchone()[0] == "1"
        long_terms = [t for t in query_terms if len(t) >= 3]
        candidates = {}
        if use_fts and long_terms:
            expression = " OR ".join('"' + t.replace('"', '""') + '"' for t in long_terms[:32])
            for row in conn.execute("SELECT p.page_id,p.active_revision,r.payload FROM wiki_fts f JOIN pages p "
                "ON p.page_id=f.page_id AND p.game_version=f.game_version AND p.locale=f.locale AND p.active_revision=f.revision "
                "JOIN page_revisions r ON r.page_id=p.page_id AND r.game_version=p.game_version AND r.locale=p.locale AND r.revision=p.active_revision "
                "WHERE wiki_fts MATCH ? AND p.game_version=? AND p.locale=? AND p.dirty=0" + filters + " ORDER BY bm25(wiki_fts),p.page_id LIMIT 100",
                [expression, game_version, locale, *filter_params]):
                candidates[row["page_id"]] = dict(row)
        # Also search short terms; trigram cannot match a two-character entity.
        short_terms = [t for t in query_terms if len(t) < 3]
        if short_terms or not candidates:
            selected = short_terms or query_terms[:16]
            where = " OR ".join("instr(json_extract(r.payload,'$.body'),?)>0 OR instr(json_extract(r.payload,'$.title'),?)>0" for _ in selected)
            params = [value for t in selected for value in (t, t)]
            for row in conn.execute("SELECT p.page_id,p.active_revision,r.payload FROM pages p JOIN page_revisions r "
                "ON r.page_id=p.page_id AND r.game_version=p.game_version AND r.locale=p.locale AND r.revision=p.active_revision "
                "WHERE p.game_version=? AND p.locale=? AND p.dirty=0 AND (" + where + ")" + filters + " ORDER BY p.page_id LIMIT 100",
                [game_version, locale, *params, *filter_params]):
                candidates[row["page_id"]] = dict(row)
        # A source may label its opening as 配队手法, with no 启动轴 token.
        # Consider reviewed curated pages only inside the explicit role scope;
        # the source-local phase matcher below still decides what can be served.
        if (document_filters and document_filters.get('category')=='guide'
                and (document_filters.get('entity') or document_filters.get('entities'))
                and re.search(r'启动轴|开局攻击轴|手法|输出轴|轮椅轴|循环|流程|连招|格挡|反击|招式衔接|【[^】]+ & [^】]+】',q)):
            for row in conn.execute("SELECT p.page_id,p.active_revision,r.payload FROM pages p JOIN page_revisions r "
                "ON r.page_id=p.page_id AND r.game_version=p.game_version AND r.locale=p.locale AND r.revision=p.active_revision "
                "WHERE p.game_version=? AND p.locale=? AND p.dirty=0 AND json_type(r.payload,'$.guide_curation')='object'"
                +filters+" ORDER BY p.page_id LIMIT 100",[game_version,locale,*filter_params]):
                candidates[row['page_id']]=dict(row)
        ranked = []
        # A held role must explain its quality status even if none of the
        # remaining metadata contains the player's gear or skill keywords.
        if (document_filters and document_filters.get('category')=='guide'
                and (document_filters.get('entity') or document_filters.get('entities'))):
            for row in conn.execute("SELECT p.page_id,p.active_revision,r.payload FROM pages p JOIN page_revisions r "
                "ON r.page_id=p.page_id AND r.game_version=p.game_version AND r.locale=p.locale AND r.revision=p.active_revision "
                "WHERE p.game_version=? AND p.locale=? AND p.dirty=0 AND json_extract(r.payload,'$.guide_quality.status')='pending_verification'"
                +filters+" ORDER BY p.page_id LIMIT 100",[game_version,locale,*filter_params]):
                candidates[row['page_id']]=dict(row)
        for row in candidates.values():
            page = json.loads(row["payload"])
            # Check the complete published source set too: guide context can
            # contain conditions outside the handful of selected claims.
            def source_current(ref):
                source = store._current_source(conn, ref['source_id'], (game_version, locale))
                return source is not None and not source['deleted'] and source['revision'] == ref['revision']

            if not page.get('sources') or not all(source_current(ref) for ref in page['sources']):
                continue
            if document_filters is not None:
                from .documents import valid_at, iso_time
                moment = document_filters["as_of"]
                meta = page["document_metadata"]
                if meta.get("source_date") and iso_time(meta["source_date"]) > moment:
                    continue
                if not document_filters.get("include_inactive") and not valid_at(meta.get("periods", []), moment):
                    continue
                eligible = [c for c in page["claims"] if document_filters.get("include_inactive") or valid_at(c.get("valid_periods", []), moment)]
                # Metadata-only claims cannot turn an expired event/mode into a positive hit.
                if meta["category"] in ("event", "endgame"):
                    eligible = [c for c in eligible if c.get("evidence_ids") != ["E001"]]
                if not eligible:
                    continue
            else:
                eligible = page["claims"]
            if entities and not set(entities).intersection(page.get("entities", [])):
                continue
            if kind and page.get("kind", "reference") != kind:
                continue
            headings = headings_for(page)
            # A skill's own section should outrank incidental mentions elsewhere;
            # otherwise a five-claim cap can omit its second form/variant.
            claim_score = lambda c: rank(c["text"], query_terms) + 0.5 * rank(headings.get(c["claim_id"], ""), query_terms)
            claims = sorted(eligible, key=lambda c: (-claim_score(c), c["claim_id"]))
            guide_topics = []
            if page.get("document_metadata",{}).get("category")=="guide":
                from .guide_selection import select_claims
                role_name=(document_filters or {}).get("query_entity_name","")
                # Keep owner names inside action notation, e.g. 绯雪E(4链),
                # so the source-pinned marker can be recognized as a quote.
                focused_query=re.sub(re.escape(role_name)+r'(?![A-Za-z])','',q).strip() if role_name else q
                if any(s['phase'] in q for f in page.get('guide_curation',{}).get('rotations',[]) for s in f['segments']):
                    focused_query=q
                if any(cue in q for f in page.get('guide_curation',{}).get('rotations',[]) for cue in f['spec'].get('playstyle',{}).get('selectors',[])):
                    focused_query=q
                selection=select_claims(page,eligible,headings,focused_query or q,terms(focused_query or q))
                if selection:
                    claims,guide_topics=selection
                elif page.get('guide_curation'):
                    # An unclassified query uses normal evidence, not a fallback
                    # that bypasses source-local phase/chain selection.
                    claims=[c for c in claims if not c['claim_id'].startswith('curation-')]
            score = max((claim_score(c) for c in claims), default=0)
            phase_selected='rotation' in guide_topics and any(c['claim_id'].startswith('curation-rotation-') for c in claims)
            scope_explanation=bool(document_filters and (document_filters.get('entity') or document_filters.get('entities')) and set(guide_topics)&{'rotation_uncovered','quality_hold'})
            if score or rank(page["title"], query_terms) or phase_selected or scope_explanation:
                ranked.append((score, row, page, claims, headings,guide_topics))
        results = []
        for score, row, page, claims, headings,guide_topics in sorted(ranked, key=lambda item: (-item[0], item[1]["page_id"])):
            matches = []
            source_refs = {}
            matched_claims = {}
            for claim in claims:
                if len(matches) >= 5:
                    break
                heading = headings.get(claim["claim_id"], "")
                if matches and not guide_topics and not rank(claim["text"] + " " + heading, query_terms):
                    break
                refs = []
                for cite in citations_for(page, claim):
                    if (page.get("kind") == "team_guide" and cite.get("source_path", [None])[0] != "excerpts"):
                        # Entity translation and version anchors remain in the
                        # audit payload, not repeated dozens of times per claim.
                        continue
                    # The parent description is enough for a tool citation. All
                    # individual parameter anchors remain available in admin audit.
                    if "param" in cite.get("source_path", []):
                        continue
                    key = (cite["source_id"], cite["revision"])
                    source = store._current_source(conn, key[0], (game_version, locale))
                    if source is None or source["deleted"] or source["revision"] != key[1]:
                        continue
                    if key not in source_refs:
                        source_refs[key] = {"source_id": key[0], "revision": key[1], "origin": source["origin"]}
                    ref = {"source_id": key[0], "revision": key[1]}
                    if cite.get("source_path") is not None:
                        ref["source_path"] = cite["source_path"]
                    if ref not in refs:
                        refs.append(ref)
                if not refs:
                    continue
                matched_claims[claim['claim_id']] = claim
                matches.append({"claim_id": claim["claim_id"], "heading": heading, "text": claim["text"], "source_refs": refs})
            if not matches:
                continue
            item = {"page_id": row["page_id"], "revision": row["active_revision"], "game_version": game_version,
                    "locale": locale, "kind": page.get("kind", "reference"), "title": page["title"],
                    "prompt_sha256": page.get("compiler", {}).get("prompt_sha256"),
                    "snippet": excerpt_snippet(matches), "matches": matches,
                    "source_refs": list(source_refs.values()), "score": score,
                    "score_kind": "lexical_overlap_not_probability", "candidate_count": len(candidates)}
            if page.get("document_metadata"):
                if page.get('guide_followup'):
                    from .guide_followup import public_context as followup_context
                    item['followup_review']=followup_context(page)
                if page.get('guide_quality'):
                    from .guide_quality import public_context
                    item['quality_review']=public_context(page)
                if page.get('guide_curation') and not is_held(page):
                    from .guide_curation import response_context
                    item['guide_context']=response_context(page,matches,q)
                if guide_topics:
                    item["excerpt_topics"]=guide_topics
                    item["excerpt_note"]="主题摘录，保留主推/替代、队伍模式与前提；未返回部分不代表原文没有。素材候选并列不证明推荐顺序。"
                item["document_metadata"] = page["document_metadata"]
                item["game_version"] = page["document_metadata"].get("applicable_game_version")
                for match in matches:
                    claim = matched_claims[match['claim_id']]
                    match["valid_periods"] = claim.get("valid_periods", [])
                    from .documents import valid_at
                    pp = match["valid_periods"]
                    moment = document_filters["as_of"] if document_filters else ""
                    match["time_status"] = ("not_applicable" if not pp else "active" if valid_at(pp, moment)
                                            else "not_started" if any(p.get("start") and p["start"] > moment for p in pp) else "ended")
                for ref in item["source_refs"]:
                    ref["origin"] = page["document_metadata"]["origin"]
            if page.get("kind") == "team_guide":
                item["scope_note"] = ("社区来源观点。当前摘录未逐项核对链数、队友配装、敌人场景与操作条件，"
                                      "不能据此保证任何配置等效或通关。复核版本不等于计算/实测版本；"
                                      "技能升级排序不等于必须先升满。仅按提问返回摘录明示的建议，不额外推导机制、阈值或差距。")
                item["scope_note"] += "只表示本次摘录未核实；不能据此说原网站未公开配装参数、输出轴或计算细节。"
            # Drop complete claims instead of cutting a trigger condition in half.
            while matches and len(json.dumps(results + [item], ensure_ascii=False)) > max_chars:
                matches.pop()
                item["snippet"] = excerpt_snippet(matches)
                used_refs = {(ref['source_id'],ref['revision']) for match in matches for ref in match['source_refs']}
                item['source_refs'] = [ref for ref in item['source_refs'] if (ref['source_id'],ref['revision']) in used_refs]
                if page.get('guide_curation') and not is_held(page):
                    item['guide_context']=response_context(page,matches,q)
            if matches:
                results.append(item)
                if len(results) >= limit:
                    break
        return results
