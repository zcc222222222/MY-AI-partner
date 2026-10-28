"""Conservative tokenizer-independent input accounting (UTF-8 bytes, not billing tokens)."""
import json


def estimate_tokens(value) -> int:
    # Byte-level tokenizers cannot normally use more tokens than input bytes.
    # Count JSON framing too; callers reserve additional protocol overhead.
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def message_cost(message) -> int:
    return estimate_tokens(message) + 16


def input_cost(messages, tools=None) -> int:
    return sum(message_cost(m) for m in messages) + estimate_tokens(tools or [])


def recent_window(history, budget):
    """Keep complete user turns, including multiple users after failed generations."""
    groups = []
    for message in history:
        if message.role == "user" or not groups:
            groups.append([])
        groups[-1].append(message)
    selected, used = [], 0
    for group in reversed(groups):
        cost = sum(message_cost({"role": m.role, "content": m.content}) for m in group)
        if used + cost > budget:
            break
        selected = group + selected
        used += cost
    if history and (not selected or selected[-1].id != history[-1].id):
        raise ValueError("当前问题超过会话输入预算，请缩小问题后重试。")
    return selected
