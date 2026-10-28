"""Bounded tool calling with explicit completion validation."""
import json
import hashlib
import logging
import re
import sqlite3
from typing import Iterator

from ..llm import stream_completion
from ..streaming import closing_iterator
from ..services.usage_service import check_cancelled
from ..config import settings
from .tools import execute_tool
from .context_budget import input_cost

logger = logging.getLogger(__name__)
MAX_TOOL_ROUNDS = 3
MAX_TOOL_RESULT_CHARS = 14000
MAX_TURN_TOOL_CHARS = 36000
WRAP_UP_PROMPT = "工具调用次数已达上限。请根据已取得的资料直接回答；证据不足的部分请明确说明，勿猜测。"
GUIDE_RESPONSE_RULES = (
    "请回到用户原问题作答。本轮配队攻略是部分摘录：只归纳实际返回的来源观点及其复核/计算版本，"
    "不要额外扩展用户未要求的机制、面板或伤害对比。未收录的内容只能说‘本次摘录未包含/未核实’，"
    "绝不能说‘攻略没写’‘原网站未公开’或‘这个数字不存在’；完整原站可能还有其他章节。"
    "技能升级优先级不表示必须先升满。不能从标签缺失推断没有机制，不能把第三方镜像称为官方数据。"
    "若指定版本缺失，当前原始库也没有该版本时，不得声称可查询其原始数据或承诺主动通知。"
)
STRUCTURED_TOOLS = {"get_character_stats", "get_character_profile", "get_ascension_materials", "search_knowledge_text",
                    "get_character_skills", "get_knowledge_entity", "get_entity_relations"}
DOCUMENT_RESPONSE_RULES = (
    "quality_review.status=pending_verification表示来源待核对，只说明资料范围，不能把该来源未返回的数值、配队或手法继续作为建议；另查机制不等于本篇已经修复。"
    "按用户原问题简洁回答。日期文档是部分摘录：平台更新时间不是游戏版本或实测时间，未知适用版本的作者攻略不能统称为3.7攻略。"
    "若检索因uncovered_phase_request只返回范围目录，而用户原问题并未限定该阶段，应在剩余工具轮次内按同一角色和原问题重查已有阶段；不要让用户再授权查一次。不得移除用户明确指定的版本、链数或阶段限制。"
    "作者文中的当前/目前指攻略当时，转述应称攻略当时，不能据此说截至今天只有某角色或某队伍。"
    "根据查询日期和条目状态区分进行中、尚未开始与已结束；未找到某活动的日期时，绝不能按同版本其他活动日期推断它已开始。"
    "只解释被问到的玩法分区，不从推荐属性推出其他属性无法通关。不因无命中说原站没有。"
    "不能承诺后台监控、未来主动回复或跨会话记住用户配置。不同元素漂泊者不能混用；同元素男女ID保留各自来源，不擅自扩展适用性。"
    "未返回的内容只能称本次摘录未包含，不能说整篇攻略只写了这些。images是应用自动附图的原站素材引用，不是图内文字识别结果，不能据此补图内机制或数值。图片类型、数量和出处由应用附注，正文不预告附图数量或逐张列图。"
    "用户仅问配队时不额外展开未问的机制或数值，不从体系不同推断成员不重叠、不能共用角色或任意候选等效。缩写只有返回内容明确支持全名时才展开，否则保留原文缩写并说明未核实，不能凭记忆猜角色。输出轴只返回概要时明确本次摘录不足，不自行补齐全流程。"
)


def _structured_version():
    try:
        from contextlib import closing
        import sqlite3
        from llmwiki.tools import KNOWLEDGE_PATH
        with closing(sqlite3.connect(KNOWLEDGE_PATH.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='game_version'").fetchone()
            return row[0] if row else None
    except (OSError, sqlite3.Error):
        return None


class IncompleteCompletionError(RuntimeError):
    """The reply must not be persisted or announced as complete."""

    def __init__(self, message: str, reason: str | None = None):
        super().__init__(message)
        self.reason = reason


def run_agent(messages: list[dict], tools: list[dict] | None = None,
              temperature: float | None = None, max_tokens: int | None = None,
              intent_routing: bool = False) -> Iterator[dict]:
    convo = list(messages)
    tool_rounds = 0
    sent_chars = 0
    seen: dict[str, str] = {}
    retried = False
    base_budget = max_tokens if max_tokens is not None else settings.llm_max_tokens
    budget = base_budget
    remaining = settings.llm_turn_max_tokens
    retry_budget = max(base_budget, settings.llm_retry_max_tokens)
    wiki_scope = None
    guide_rules_sent = False
    document_rules_sent = False
    impact_rules_sent = False
    system_sha256 = hashlib.sha256("\n".join(m.get("content", "") for m in messages if m.get("role") == "system").encode()).hexdigest()
    plan = None
    if intent_routing:
        from .intent import resolve_intent, allowed_tools, guidance
        # Do not spend an extra network call on a request that cannot fit anyway.
        if input_cost(convo, tools) + budget + settings.llm_context_safety_tokens > settings.llm_context_window_tokens:
            raise IncompleteCompletionError("本轮上下文预算不足，请缩小问题。", reason="context_budget")
        plan = resolve_intent(messages)
        if plan:
            tools = allowed_tools(plan, tools)
            wiki_scope = plan.get("game_version")
            trace = {"format":plan["format"],"status":plan["status"],"topics":plan["topics"],
                     "entity_keys":[e["key"] for e in plan["entities"]],"image_mode":plan["image_mode"],
                     "ambiguous":plan["ambiguous"],"game_version":wiki_scope,"include_historical":plan.get('include_historical',False),
                     "tools":[t["function"]["name"] for t in tools or []]}
            logger.info("intent.route %s",json.dumps(trace,ensure_ascii=False))
            yield {"type":"intent_trace","trace":trace}
            # Old images in assistant history are display artifacts, not input facts.
            convo = [{**m,"content":re.split(r"\n\n相关原图：",m["content"])[0]}
                     if m.get("role")=="assistant" and isinstance(m.get("content"),str) else m for m in convo]
            convo.append({"role":"user","content":guidance(plan)})
    while True:
        check_cancelled()
        active_tools = tools if tool_rounds < MAX_TOOL_ROUNDS else None
        # Per-call caps do not force consumption; cap total output across the turn too.
        if tools and tool_rounds >= MAX_TOOL_ROUNDS:
            budget = max(budget, settings.llm_final_max_tokens)
        budget = min(budget, remaining)
        if budget <= 0:
            raise IncompleteCompletionError("本轮生成预算已用完，请拆小问题。", reason="turn_budget")
        estimated = input_cost(convo, active_tools)
        if estimated + budget + settings.llm_context_safety_tokens > settings.llm_context_window_tokens:
            raise IncompleteCompletionError("本轮上下文预算不足，请缩小问题。", reason="context_budget")
        logger.info("context.call estimated_bytes=%d output_reserve=%d envelope=%d tool_round=%d",
                    estimated, budget, settings.llm_context_window_tokens, tool_rounds)
        content = []
        calls = None
        completion = None
        output_tokens = None
        with closing_iterator(stream_completion(convo, tools=active_tools,
                                       temperature=temperature, max_tokens=budget)) as events:
            for event in events:
                if event["type"] == "delta":
                    content.append(event["content"])
                    yield event
                elif event["type"] == "usage":
                    output_tokens = event["usage"].get("completion_tokens")
                    logger.info("completion.usage usage=%s", event["usage"])
                    yield event
                elif event["type"] == "tool_calls":
                    calls = event["tool_calls"]
                elif event["type"] == "completion":
                    completion = event
        reason = completion.get("finish_reason") if completion else None
        # Endpoints without usage are charged the requested cap conservatively.
        remaining -= max(0, int(output_tokens)) if output_tokens is not None else budget
        logger.info("completion finish_reason=%s max_tokens=%s visible_chars=%d tool_calls=%d",
                    reason, budget, sum(map(len, content)), len(calls or []))
        yield {"type": "generation", "finish_reason": reason, "max_tokens": budget,
               "tool_round": tool_rounds, "remaining_output_tokens": remaining}
        if reason in ("length", "stop") and not "".join(content).strip() and not calls and not retried and budget < retry_budget and remaining > budget:
            retried = True
            budget = min(retry_budget, remaining)
            yield {"type": "retry", "reason": "empty_" + reason, "max_tokens": budget}
            continue
        if reason not in ("stop", "tool_calls"):
            detail = "已达到本次生成 token 上限" if reason == "length" else "生成流没有正常结束"
            raise IncompleteCompletionError(f"{detail}，回答尚未完成，请缩小问题后重试。", reason=reason)
        if not calls:
            if reason != "stop" or not "".join(content).strip():
                raise IncompleteCompletionError("模型未生成完整回答，请重试。", reason="empty")
            return
        if reason != "tool_calls" or not active_tools:
            raise IncompleteCompletionError("工具调用未正常完成，请重试。")
        assistant = {"role": "assistant", "content": "".join(content) or None, "tool_calls": calls}
        if completion.get("reasoning_content"):
            assistant["reasoning_content"] = completion["reasoning_content"]
        convo.append(assistant)
        tool_rounds += 1
        guide_rules_pending = False
        document_rules_pending = False
        impact_rules_pending = False
        live_rules_pending = False
        for tc in calls:
            function = tc["function"]
            effective_arguments = function["arguments"]
            scope_error = None
            try:
                parsed_arguments = json.loads(effective_arguments or "{}")
                if plan:
                    from .intent import scoped_arguments
                    allowed_names = {t["function"]["name"] for t in active_tools or []}
                    if function["name"] not in allowed_names:
                        scope_error = "该工具不属于本轮意图允许的检索通道，请使用已提供的工具或先澄清。"
                    try:
                        parsed_arguments = scoped_arguments(plan,function["name"],parsed_arguments)
                        effective_arguments = json.dumps(parsed_arguments,ensure_ascii=False)
                    except (ValueError, OSError, sqlite3.Error):
                        scope_error = "工具参数不符合本轮确认的实体、作者、日期或版本限制，请核对后重试或澄清。"
                if function["name"] in ("search_wiki", "search_documents") and isinstance(parsed_arguments, dict):
                    if wiki_scope and not (plan and plan["status"]=="parsed" and not plan.get("game_version") and function["name"]=="search_documents"):
                        parsed_arguments["game_version"] = wiki_scope
                        effective_arguments = json.dumps(parsed_arguments, ensure_ascii=False)
                    elif isinstance(parsed_arguments.get("game_version"), str) and re.fullmatch(r"\d+(?:\.\d+){1,2}", parsed_arguments["game_version"]):
                        wiki_scope = parsed_arguments["game_version"]
                canonical = json.dumps(parsed_arguments, sort_keys=True)
            except (ValueError, TypeError):
                canonical = function["arguments"]
            key = function["name"] + ":" + canonical
            original_chars = 0
            if key in seen:
                result = json.dumps({"already_returned": True, "tool_call_id": seen[key],
                                     "note": "同一结果已在本轮上下文中，请复用。"}, ensure_ascii=False)
            else:
                if scope_error:
                    result = json.dumps({"error":"intent_scope_mismatch","note":scope_error},ensure_ascii=False)
                elif wiki_scope and function["name"] in STRUCTURED_TOOLS and _structured_version() != wiki_scope:
                    result = json.dumps({"error": "knowledge_version_mismatch", "game_version": wiki_scope,
                                         "note": "本轮Wiki查询已锁定该版本，当前SQLite版本不同或无法确认，不能用其他版本补答案。请直接说明指定版本资料不足。"}, ensure_ascii=False)
                else:
                    if function['name'] == 'find_gameplay_guides':
                        yield {'type':'tool_status','message':'正在查询玩法攻略…'}
                    check_cancelled()
                    result = execute_tool(function["name"], effective_arguments)
                if plan and plan["image_mode"]=="none" and function["name"]=="search_documents":
                    payload=json.loads(result)
                    for page in payload.get("results",[]): page.pop("images",None)
                    result=json.dumps(payload,ensure_ascii=False)
                original_chars = len(result)
                future = {"role": "tool", "tool_call_id": tc["id"], "content": result}
                reserve = max(base_budget, settings.llm_final_max_tokens, settings.llm_retry_max_tokens)
                if (len(result) > MAX_TOOL_RESULT_CHARS or sent_chars + len(result) > MAX_TURN_TOOL_CHARS
                        or input_cost(convo + [future], tools) + reserve + settings.llm_context_safety_tokens + 512
                        > settings.llm_context_window_tokens):
                    result = json.dumps({"error": "tool_payload_budget", "original_chars": len(result),
                                         "note": "结果未注入上下文，请指定技能 tree_id、chain_no、offset 或缩小查询。不能据此推断无资料。"},
                                        ensure_ascii=False)
                else:
                    seen[key] = tc["id"]
            sent_chars += len(result)
            logger.info("tool_payload name=%s original_chars=%d sent_chars=%d turn_chars=%d",
                        function["name"], original_chars, len(result), sent_chars)
            yield {"type": "tool_metrics", "name": function["name"], "original_chars": original_chars,
                   "sent_chars": len(result), "turn_chars": sent_chars, "duplicate": original_chars == 0}
            if function['name'] == 'find_gameplay_guides':
                payload = json.loads(result)
                if not payload.get('already_returned'):
                    wait = payload.get('retry_after_seconds',0)
                    status_text = ('已取得攻略，正在整理回答…' if payload.get('status') == 'links_found' else
                        f'本次未取得攻略，{wait}秒后可重试，正在整理说明…' if type(wait) is int and wait > 0 else
                        '查询已结束，正在整理结果…')
                    yield {'type':'tool_status','message':status_text}
                live_rules_pending = True
                sources = []
                if payload.get('status') == 'links_found':
                    sources = [{**{k: p.get(k) for k in ('title', 'url', 'date_label', 'game_version', 'author', 'post_id', 'images', 'snapshot_notice')},
                                'source_kind': 'live_kurobbs', 'presentation': payload.get('presentation', 'article')}
                               for p in payload.get('results', [])]
                elif payload.get('status') == 'reference_available':
                    sources = [{**{k: p.get(k) for k in ('title', 'url', 'date_label', 'author', 'post_id')},
                                'source_kind': 'provided_weibo_reference', 'presentation': 'links'}
                               for p in payload.get('results', [])]
                elif payload.get('status') == 'period_unverified':
                    sources = [{**{k: p.get(k) for k in ('title', 'url', 'game_version', 'author', 'post_id', 'images')},
                                'source_kind': 'live_kurobbs', 'presentation': payload.get('presentation', 'article'),
                                'date_label': '周期未核实候选 · ' + str(p.get('date_label', ''))}
                               for p in payload.get('candidates', [])]
                yield {'type': 'knowledge_trace', 'trace': {'route': 'live_guides',
                       'status': payload.get('status', payload.get('error', 'duplicate')),
                       'delivered_chars': len(result), 'tool_call_id': tc['id'],
                       'system_sha256': system_sha256}, 'sources': sources}
            if function['name']=='get_version_impact':
                payload=json.loads(result)
                trace={**payload.get('trace',{}),'route':'version_impacts','status':payload.get('status',payload.get('error','duplicate')),
                       'delivered_chars':len(result),'tool_call_id':tc['id'],'system_sha256':system_sha256}
                sources=[{'title':p['title'],'url':p['url'],'date_label':'发布 '+p['source_date'],
                          'game_version':p['game_version'],'source_id':p['source_id'],'source_revision':p['source_revision']}
                         for p in payload.get('results',[])]
                yield {'type':'knowledge_trace','trace':trace,'sources':sources}
                impact_rules_pending=True
            if function["name"] in ("search_wiki", "search_documents"):
                payload = json.loads(result)
                document_rules_pending = document_rules_pending or function["name"] == "search_documents"
                guide_rules_pending = guide_rules_pending or any(p.get("kind") == "team_guide" for p in payload.get("results", []))
                if not wiki_scope and payload.get("game_version"):
                    wiki_scope = payload["game_version"]
                trace = {**payload.get("trace", {}), "route": "documents" if function["name"] == "search_documents" else "wiki", "status": payload.get("status", payload.get("error", "duplicate")),
                         "delivered_chars": len(result), "tool_call_id": tc["id"], "system_sha256": system_sha256}
                sources = []
                for page in payload.get("results", []):
                    for review in page.get('impact_reviews',[]):
                        n=review['notice_source']
                        sources.append({'title':n['title'],'game_version':n['game_version'],'date_label':'发布 '+n['date'],
                                        'url':n['url'],'source_id':n['source_id'],'source_revision':n['revision']})
                    for source in page.get("source_refs", []):
                        if source.get("origin"):
                            meta = page.get("document_metadata", {})
                            date_label = (("平台更新 " if meta.get("date_basis") == "platform_modifiedAt" else "发布 ") + str(meta["source_date"])) if meta.get("source_date") else "更新时间未提供"
                            sources.append({"title": page["title"], "game_version": page["game_version"],
                                            "date_label": date_label if meta else None, "license_note": meta.get("license_note"),
                                            "url": source["origin"], "source_id": source["source_id"], "source_revision": source["revision"]})
                            if page.get("images"):
                                sources[-1]["images"] = [i for i in page["images"] if i["source_id"]==source["source_id"] and i["source_revision"]==source["revision"]]
                logger.info("retrieval.delivered %s", json.dumps(trace, ensure_ascii=False))
                yield {"type": "knowledge_trace", "trace": trace, "sources": sources}
            convo.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
        if guide_rules_pending and not guide_rules_sent:
            # Stable app guidance at the tail; keep persona/system unchanged.
            convo.append({"role": "user", "content": GUIDE_RESPONSE_RULES})
            guide_rules_sent = True
        if document_rules_pending and not document_rules_sent:
            convo.append({"role": "user", "content": DOCUMENT_RESPONSE_RULES})
            document_rules_sent = True
        if impact_rules_pending and not impact_rules_sent:
            from llmwiki.notice_impacts import POLICY as impact_policy
            convo.append({'role':'user','content':'【公告影响回答边界】'+impact_policy})
            impact_rules_sent=True
        if live_rules_pending:
            from .live_guides import POLICY
            convo.append({'role': 'user', 'content': '【联网攻略回答边界】' + POLICY})
        if tool_rounds >= MAX_TOOL_ROUNDS:
            convo.append({"role": "user", "content": WRAP_UP_PROMPT})
        budget = base_budget
