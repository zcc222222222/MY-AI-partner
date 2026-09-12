"""DeepSeek 客户端封装:统一流式 / 非流式调用入口。"""
from typing import Any, Iterator

from openai import OpenAI

from .config import settings

_client = OpenAI(api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url)


def chat(messages: list[dict], model: str | None = None) -> str:
    """非流式补全,用于标题生成等短任务。"""
    response = _client.chat.completions.create(
        model=model or settings.deepseek_model,
        messages=messages,
    )
    return response.choices[0].message.content or ""


def stream_completion(messages: list[dict], tools: list[dict] | None = None) -> Iterator[dict]:
    """流式补全。

    产出两种事件:
      {"type": "delta", "content": str}              文本增量
      {"type": "tool_calls", "tool_calls": [...]}    流结束时模型决定调用工具
    """
    kwargs: dict[str, Any] = {"model": settings.deepseek_model, "messages": messages, "stream": True}
    if tools:
        kwargs["tools"] = tools

    response = _client.chat.completions.create(**kwargs)

    tool_calls: dict[int, dict] = {}
    for chunk in response:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta and delta.content:
            yield {"type": "delta", "content": delta.content}
        if delta and getattr(delta, "tool_calls", None):
            for tc in delta.tool_calls:
                idx = tc.index
                entry = tool_calls.setdefault(idx, {
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                })
                if tc.id:
                    entry["id"] = tc.id
                if tc.function:
                    if tc.function.name:
                        entry["function"]["name"] = tc.function.name
                    if tc.function.arguments:
                        entry["function"]["arguments"] += tc.function.arguments

    if tool_calls:
        yield {"type": "tool_calls", "tool_calls": [tool_calls[i] for i in sorted(tool_calls)]}
