"""DeepSeek 客户端封装:统一流式 / 非流式调用入口。"""
import logging
from typing import Any, Iterator

from openai import OpenAI

from .config import settings

logger = logging.getLogger(__name__)

_client = OpenAI(api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url)


def chat(messages: list[dict], model: str | None = None) -> str:
    """非流式补全,用于标题生成等短任务。"""
    response = _client.chat.completions.create(
        model=model or settings.deepseek_model,
        messages=messages,
    )
    return response.choices[0].message.content or ""


def stream_completion(
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> Iterator[dict]:
    """流式补全。

    产出三种事件:
      {"type": "delta", "content": str}              文本增量
      {"type": "tool_calls", "tool_calls": [...]}    流结束时模型决定调用工具
      {"type": "usage", "usage": {...}}              本次调用的用量(成本核算用)
    """
    kwargs: dict[str, Any] = {
        "model": settings.deepseek_model,
        "messages": messages,
        "stream": True,
        # 开启后流的最后一个 chunk 才会带 usage;否则拿不到 token 用量。
        "stream_options": {"include_usage": True},
    }
    if tools:
        kwargs["tools"] = tools
    if temperature is not None:
        kwargs["temperature"] = temperature
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens

    try:
        response = _client.chat.completions.create(**kwargs)
    except Exception as exc:  # noqa: BLE001
        # 少数兼容端点不认 stream_options。宁可丢用量统计,也不能让聊天整体不可用。
        # 只在这个参数确实被拒绝时重试,避免把鉴权失败也重发一遍。
        text = str(exc).lower()
        if "stream_options" not in text and not isinstance(exc, TypeError):
            raise
        logger.warning("端点不接受 stream_options,退回不带用量的调用: %s", exc)
        kwargs.pop("stream_options")
        response = _client.chat.completions.create(**kwargs)

    tool_calls: dict[int, dict] = {}
    usage: dict | None = None

    for chunk in response:
        # 用量块的 choices 是空的,必须在下面的判空之前取,否则会被 continue 跳过。
        if getattr(chunk, "usage", None):
            usage = chunk.usage.model_dump()

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
    if usage:
        yield {"type": "usage", "usage": usage}
