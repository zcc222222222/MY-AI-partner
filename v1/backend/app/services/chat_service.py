"""聊天服务:组装角色扮演上下文并流式返回回复。"""
import json
import logging
from typing import Iterator

from sqlalchemy.orm import Session

from .. import models
from ..agents.orchestrator import run_agent
from ..agents.roleplay import build_context, build_system_prompt
from ..agents.tools import TOOLS
from ..config import settings
from . import session_service

logger = logging.getLogger(__name__)


def stream_reply(
    db: Session,
    session: models.ChatSession,
    character: models.Character,
    user_content: str,
) -> Iterator[str]:
    """持久化用户消息,流式生成助手回复并持久化。

    产出的是 SSE 的 data 载荷(JSON 字符串),由路由层包装成 SSE 帧。
    """
    session_service.append_message(db, session.id, "user", user_content)

    history = session_service.list_messages(db, session.id)
    system_prompt = build_system_prompt(character)
    messages = [{"role": "system", "content": system_prompt}, *build_context(history)]

    full_text = ""
    usages: list[dict] = []
    for event in run_agent(
        messages,
        tools=TOOLS,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
    ):
        if event["type"] == "delta":
            full_text += event["content"]
            yield json.dumps({"type": "delta", "content": event["content"]}, ensure_ascii=False)
        elif event["type"] == "usage":
            usages.append(event["usage"])

    # 用量埋点。重点看 prompt_cache_hit_tokens:连发两轮后它应 > 0,
    # 一直是 0 说明 system prompt 前缀被破坏,输入成本会高出约 10 倍。
    logger.info(
        "chat.completed session_id=%s model=%s usage=%s",
        session.id,
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

    assistant = session_service.append_message(db, session.id, "assistant", full_text)

    # 先把 done 发出去:回答已经完整到达,前端应当立刻解锁输入框,
    # 不要为了下面那次「生成标题」的额外 LLM 调用再干等几秒。
    yield json.dumps({"type": "done", "message_id": assistant.id}, ensure_ascii=False)

    user_turns = sum(1 for m in history if m.role == "user")
    if user_turns == 1:
        title = session_service.generate_title(user_content)
        session.title = title
        db.commit()
        yield json.dumps({"type": "title", "title": title}, ensure_ascii=False)
