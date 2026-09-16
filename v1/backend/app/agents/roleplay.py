"""角色扮演 Agent:加载版本化角色卡,拼接系统提示词与对话上下文。"""
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


# ⚠️ 前缀稳定性是成本红线,不要破坏:
# DeepSeek 会对重复的 prompt 前缀做自动上下文缓存(命中价约为未命中的 1/10)。
# 因此 system prompt 必须逐字节稳定 —— 禁止把检索结果、时间戳、随机内容拼进它。
# v2 接入 LLM Wiki 检索时,检索片段必须作为 tool result 追加在对话尾部。
# 一旦插进 system prompt,缓存命中率归零,输入成本直接上涨约 10 倍。
def build_context(history: list[Any], *, budget: int | None = None) -> list[dict]:
    """把会话历史转成发给模型的 messages。

    v1:全量透传(budget 预留,暂不生效)。
    v2:在这里实现按 budget 的滑窗 + 摘要压缩;调用方签名不变,只改这一个函数。
    """
    return [{"role": m.role, "content": m.content} for m in history]
