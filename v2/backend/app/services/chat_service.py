"""聊天服务:组装角色扮演上下文并流式返回回复。"""
import json
import logging
from typing import Iterator
from types import SimpleNamespace

from sqlalchemy.orm import Session

from .. import models
from ..agents.orchestrator import IncompleteCompletionError, run_agent
from ..agents.roleplay import build_system_prompt
from ..agents.tools import TOOLS
from ..config import settings
from ..streaming import closing_iterator
from . import session_service, context_service

logger = logging.getLogger(__name__)


def _grounded_text(text):
    """Remove observed unsupported background-notification promises from knowledge replies."""
    import re
    parts=re.split(r'(?<=[。！？!?])',text)
    kept=[]
    for part in parts:
        promise=re.search(r'第一个告诉你|(?:我会|我来|我帮你|到时|到时候).{0,16}(?:主动通知|自动通知|通知你|联系你)|(?:资料齐|资料更新).{0,20}(?:告诉你|通知)',part)
        denied=re.search(r'不能|不会|无法|不支持|没有.{0,5}功能',part)
        if promise and not denied and not part.lstrip().startswith(('>','```')):
            continue
        kept.append(part)
    return ''.join(kept).strip() or '本次没有可核实的资料，暂时无法给出可靠结论。'
BUDGET_FALLBACK = (
    "这次回答没能完整生成，暂时无法给出可靠的完整结论。"
    "请把问题拆小后再问，例如先确认一个技能的触发条件，再讨论配队。"
    "本次未完成的内容不作为攻略结论。"
)


def _source_suffix(sources, answer):
    """Cite only actually delivered Wiki results; never expose local file paths."""
    from urllib.parse import urlsplit
    links, seen = [], set()
    for source in sources:
        if source.get('source_kind') == 'live_kurobbs' and source.get('presentation') == 'article':
            label = ' · '.join(str(source.get(k) or '') for k in ('author', 'date_label'))
            if label not in seen:
                links.append('库街区 · ' + label.replace('\n', ' ').replace('[', '（').replace(']', '）'))
                seen.add(label)
            continue
        url = source.get("url", "")
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            continue
        version = source.get("date_label") or source.get("game_version", "")
        key = (url, version)
        if key in seen or (url in answer and "CC BY-NC-SA" not in str(source.get("license_note", ""))):
            continue
        seen.add(key)
        title = str(source.get("title", "Wiki资料"))[:80].replace("[", "（").replace("]", "）").replace("\n", " ")
        # Angle-bracket URL form keeps parentheses in an upstream URL valid.
        safe_url = url.replace("<", "%3C").replace(">", "%3E").replace("\n", "%0A").replace("\r", "%0D")
        credit = " · 鸣潮BWIKI贡献者 · CC BY-NC-SA 4.0" if "CC BY-NC-SA" in str(source.get("license_note", "")) else ""
        links.append(f"[{title} · {version}{credit}](<{safe_url}>)")
        if len(links) == 5:
            break
    notices = list(dict.fromkeys(str(s['snapshot_notice']) for s in sources
                                if s.get('source_kind')=='live_kurobbs' and s.get('snapshot_notice')))
    suffix = "\n\n资料来源：" + "；".join(links) if links else ""
    # Guaranteed attribution survives a model omitting the tool's freshness instruction.
    return suffix + ''.join('\n\n'+notice for notice in notices)


def _with_budget_fallback(events):
    try:
        yield from events
    except IncompleteCompletionError as exc:
        if exc.reason not in ("length", "turn_budget", "empty", "context_budget"):
            raise
        yield {"type": "replace", "content": BUDGET_FALLBACK}


def _image_suffix(sources, answer):
    """Append only images actually delivered in tools, and revalidate before persistence."""
    if not any(source.get("images") for source in sources): return ""
    from ..agents.guide_content import image_url
    live_blocks, live_seen = [], set()
    for source in sources:
        if len(live_blocks) >= 8: break
        if source.get('source_kind') != 'live_kurobbs' or source.get('presentation') != 'article': continue
        for ref in source.get('images') or []:
            url = image_url(ref.get('url'))
            if not url or not source.get('post_id') or ref.get('post_id') != source['post_id'] or url in live_seen: continue
            live_seen.add(url)
            live_blocks.append(f"![作者原图 {len(live_blocks)+1}]({url})")
            if len(live_blocks) >= 8: break
    if live_blocks:
        return '\n\n作者原图：\n\n' + '\n\n'.join(live_blocks)
    from llmwiki import WikiStore
    from llmwiki import tools
    from llmwiki.images import resolve_image, IMAGE_ROUTE
    from urllib.parse import urlsplit
    import re
    import sqlite3
    def plain(value):
        text = re.sub(r"[\r\n\t]", " ", str(value or ""))[:120]
        return re.sub(r"([\\`*_!{}\[\]()<>])", r"\\\1", text)
    blocks, seen = [], set()
    try:
        store = WikiStore(tools.WIKI_PATH,create=False,read_only=True)
        for source in sources:
            for ref in source.get("images") or []:
                image = resolve_image(store,ref.get("image_id"))
                if (not image or image["source_id"]!=source.get("source_id") or image["source_revision"]!=source.get("source_revision")
                        or image["source_id"]!=ref.get("source_id") or image["source_revision"]!=ref.get("source_revision") or image["url"] in seen):
                    continue
                url = IMAGE_ROUTE + image["image_id"]
                if url in answer: continue
                origin = image["origin"]
                parsed = urlsplit(origin)
                if parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password: continue
                origin = origin.replace("<","%3C").replace(">","%3E").replace("\n","%0A").replace("\r","%0D")
                date = ("平台更新 " if image["date_basis"]=="platform_modifiedAt" else "发布 ") + str(image["source_date"])[:10] if image["source_date"] else "更新时间未提供"
                credit = " · 鸣潮BWIKI贡献者 · CC BY-NC-SA 4.0" if "CC BY-NC-SA" in image["license_note"] else ""
                blocks.append(f"[![{plain(image['label'])}]({url})]({url})\n\n{plain(image['label'])} · 来源：{plain(image['author'])} · {plain(date)}{credit} · [查看来源](<{origin}>)")
                seen.add(image["url"])
                if len(blocks)==2: return "\n\n相关原图：\n\n"+"\n\n".join(blocks)
    except (OSError, ValueError, sqlite3.Error):
        logger.info("knowledge.images unavailable; keeping text reply")
    return "\n\n相关原图：\n\n"+"\n\n".join(blocks) if blocks else ""


def stream_reply(
    db: Session,
    session: models.ChatSession,
    character: models.Character,
    user_content: str,
    *, authorize=None,
) -> Iterator[str]:
    """持久化用户消息,流式生成助手回复并持久化。

    产出的是 SSE 的 data 载荷(JSON 字符串),由路由层包装成 SSE 帧。
    """
    if authorize:
        authorize()
    session_id, owner_id = session.id, session.owner_id
    user_message = session_service.append_message(db, session_id, "user", user_content, owner_id=owner_id)
    from . import usage_service
    if usage_service.CURRENT.get():
        message_id = user_message.id
        db.rollback()
        usage_service.record_user(message_id)

    history = session_service.list_messages(db, session_id, owner_id=owner_id)
    history = usage_service.context_history(db, history, owner_id, session_id)
    system_prompt = build_system_prompt(character)
    user_turns = sum(1 for m in history if m.role == "user")
    # Snapshot values before rollback expires SQLAlchemy instances.
    history = [SimpleNamespace(id=m.id, role=m.role, content=m.content) for m in history]
    context = context_service.prepare_context(db, history, session_id=session_id, owner_id=owner_id,
        system_prompt=system_prompt, tools=TOOLS, authorize=authorize)
    messages = [{"role": "system", "content": system_prompt}, *context]
    # Release the read transaction before waiting on the model (notably MySQL).
    db.rollback()

    full_text = ""
    usages: list[dict] = []
    fallback = False
    knowledge_sources = []
    image_mode = "auto"
    knowledge_requested = False
    with closing_iterator(_with_budget_fallback(run_agent(
        messages,
        tools=TOOLS,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        intent_routing=True,
    ))) as events:
        for event in events:
            if event["type"] == "delta":
                full_text += event["content"]
                yield json.dumps({"type": "delta", "content": event["content"]}, ensure_ascii=False)
            elif event["type"] == "usage":
                usages.append(event["usage"])
            elif event['type'] == 'tool_status':
                yield json.dumps({'type':'tool_status','message':event['message']},ensure_ascii=False)
            elif event["type"] == "replace":
                fallback = True
                full_text = event["content"]
                yield json.dumps(event, ensure_ascii=False)
            elif event["type"] == "knowledge_trace":
                knowledge_requested = True
                knowledge_sources.extend(event.get("sources", []))
            elif event["type"] == "intent_trace":
                image_mode = event["trace"]["image_mode"]

    if full_text.strip() and not fallback:
        if knowledge_requested:
            corrected = _grounded_text(full_text)
            # Presentation is an application contract: no post hyperlinks in article replies.
            import re
            for source in knowledge_sources:
                if source.get('source_kind') != 'live_kurobbs' or source.get('presentation') != 'article': continue
                url = source.get('url', '')
                if not url: continue
                corrected = re.sub(r'\[([^\]]+)\]\(<?' + re.escape(url) + r'>?\)', r'\1', corrected)
                corrected = re.sub(r'^.*(?:链接|出处)[：:]\s*' + re.escape(url) + r'.*$', '', corrected, flags=re.M)
                corrected = corrected.replace(url, '')
            if corrected != full_text:
                full_text = corrected
                yield json.dumps({"type":"replace","content":full_text},ensure_ascii=False)
        suffix = _source_suffix(knowledge_sources, full_text)
        if image_mode != "none":
            suffix += _image_suffix(knowledge_sources, full_text)
        if suffix:
            full_text += suffix
            yield json.dumps({"type": "delta", "content": suffix}, ensure_ascii=False)

    # 用量埋点。重点看 prompt_cache_hit_tokens:连发两轮后它应 > 0,
    # 一直是 0 说明 system prompt 前缀被破坏,输入成本会高出约 10 倍。
    logger.info(
        "chat.fallback session_id=%s model=%s usage=%s" if fallback
        else "chat.completed session_id=%s model=%s usage=%s",
        session_id,
        settings.deepseek_model,
        usages,
    )

    if not full_text.strip():
        # 收口之后仍然没有正文 —— 宁可让用户重试,也不要写一条空消息进库:
        # 那会变成一条每次进会话都重现的空气泡。
        yield json.dumps(
            {"type": "error", "detail": "模型这次没有生成出内容,请重试"},
            ensure_ascii=False,
        )
        return

    if authorize:
        authorize()
    assistant = session_service.append_message(db, session_id, "assistant", full_text, owner_id=owner_id)
    assistant_id = assistant.id
    db.rollback()  # Do not hold a read snapshot while the separately metered title runs.
    # Persist completion identity before the network can disconnect at done.
    if usage_service.CURRENT.get():
        usage_service.record_answer(usage_service.CURRENT.get(), assistant_id)

    # 先把 done 发出去:回答已经完整到达,前端应当立刻解锁输入框,
    # 不要为了下面那次「生成标题」的额外 LLM 调用再干等几秒。
    yield json.dumps({"type": "done", "message_id": assistant_id,
                      "status": "fallback" if fallback else "complete"}, ensure_ascii=False)

    if user_turns == 1 and not fallback:
        if authorize:
            authorize()
        title = session_service.generate_title(user_content)
        if authorize:
            authorize()
        session.title = title
        db.commit()
        yield json.dumps({"type": "title", "title": title}, ensure_ascii=False)
