"""角色扮演 Agent:加载版本化角色卡,拼接系统提示词与记忆。"""
from typing import Any


def build_system_prompt(
    character: Any,
    nickname: str | None = None,
    personality: str | None = None,
) -> str:
    nickname = nickname or character.display_name
    personality = personality or character.personality or ""
    return (
        character.system_prompt
        .replace("{nickname}", nickname)
        .replace("{personality}", personality)
    )


def build_memory(history: list[Any]) -> list[dict]:
    """把会话历史转成发给模型的 messages。"""
    return [{"role": m.role, "content": m.content} for m in history]
