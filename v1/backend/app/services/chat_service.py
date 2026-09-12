"""聊天服务:组装角色扮演上下文并流式返回回复。"""
import json
from typing import Iterator

from sqlalchemy.orm import Session

from .. import models
from ..agents.orchestrator import run_agent
from ..agents.roleplay import build_memory, build_system_prompt
from ..agents.tools import TOOLS
from . import session_service


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
    messages = [{"role": "system", "content": system_prompt}, *build_memory(history)]

    full_text = ""
    for event in run_agent(messages, tools=TOOLS):
        if event["type"] == "delta":
            full_text += event["content"]
            yield json.dumps({"type": "delta", "content": event["content"]}, ensure_ascii=False)

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
