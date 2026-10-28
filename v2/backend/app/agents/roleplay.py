"""角色扮演 Agent:加载版本化角色卡,拼接系统提示词与对话上下文。"""
from typing import Any


# 固定输出规范，所有请求复用相同文本，保持系统前缀缓存稳定。
MARKDOWN_STYLE = """

【回答排版】
使用 Markdown 输出可直接渲染的正文，不要把整段回答包进 ```markdown 代码围栏，也不要输出 HTML 标签。
先直接回答玩家最关心的结论，再补必要的理由、条件和操作步骤。简单问题用短段落即可；较长攻略按需使用 ##、### 小标题，不要为了排版重复结论或强行套模板。
段落、标题、列表及表格前后留空行；用 **加粗** 突出少量关键条件、推荐或注意事项，避免整段加粗。
顺序操作用有序列表，并列建议用无序列表；配装、角色或方案的多项对比才使用简短表格，单元格避免塞入长篇解释。
只有真正的代码才使用带语言名的代码块；游戏连招使用普通步骤或短行内代码，避免长行。来源按已有检索规则标注，用 [来源名称](已核实的链接) 表达链接；图片仅使用工具提供的已核实地址，不编造来源或图片。
保留角色语气，但优先保证答案清楚、条件准确和证据边界明确。
"""


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
    ) + MARKDOWN_STYLE


# ⚠️ 前缀稳定性是成本红线,不要破坏:
# DeepSeek 会对重复的 prompt 前缀做自动上下文缓存(命中价约为未命中的 1/10)。
# 因此 system prompt 必须逐字节稳定 —— 禁止把检索结果、时间戳、随机内容拼进它。
# v2 接入 LLM Wiki 检索时,检索片段必须作为 tool result 追加在对话尾部。
# 一旦插进 system prompt,缓存命中率归零,输入成本直接上涨约 10 倍。
def build_context(history: list[Any], *, budget: int | None = None) -> list[dict]:
    """Convert history, keeping complete recent turns within a conservative budget."""
    if budget is not None:
        from .context_budget import recent_window
        history = recent_window(history, budget)
    return [{"role": m.role, "content": m.content} for m in history]
