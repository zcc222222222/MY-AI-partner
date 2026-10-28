"""DeepSeek 客户端封装:统一流式 / 非流式调用入口。"""
import logging
from functools import lru_cache
from typing import Any, Iterator

from openai import OpenAI

from .config import settings
from .services import usage_service as metering

logger = logging.getLogger(__name__)

# SDK 默认超时是 600s —— 一个挂死的请求会占住一个线程最长 10 分钟。
# 这是 httpx 的读超时(两次数据之间的间隔),正常的长回答只要还在吐字就不会触发。
LLM_TIMEOUT_SECONDS = 60.0
LLM_MAX_RETRIES = 2


@lru_cache(maxsize=1)
def _get_client() -> OpenAI:
    """惰性构造客户端。

    不能在模块级构造:openai SDK 把**空字符串**当成「缺凭证」,在 import 阶段
    就抛 OpenAIError —— 英文、还叫你去设 `OPENAI_API_KEY`,而 main.py lifespan
    里那句中文 fail-fast 永远执行不到。空密钥恰恰是最常见的场景(没建 .env、
    或者把密钥那一行注释掉),报错提示却把排查方向带偏到另一个项目上。

    惰性之后,「密钥是否可用」这件事只由 lifespan 一处负责判断。
    """
    return OpenAI(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        timeout=LLM_TIMEOUT_SECONDS,
        max_retries=LLM_MAX_RETRIES,
    )


def chat(messages: list[dict], model: str | None = None) -> str:
    """非流式短任务（当前仅标题）：小预算、关闭思考。"""
    response = completion(purpose='title',
        model=model or settings.deepseek_model,
        messages=messages,
        max_tokens=128,
        extra_body={"thinking": {"type": "disabled"}},
    )
    if response.choices[0].finish_reason != "stop":
        raise RuntimeError("标题生成未正常完成")
    return response.choices[0].message.content or ""


def completion(*,purpose,timeout=None,**kwargs):
    """Meter every short completion, including parsing, memory and title calls."""
    call=metering.reserve_call(kwargs,purpose)
    usage=None
    try:
        client=_get_client()
        options={}
        if timeout is not None:options['timeout']=timeout
        if call or timeout is not None:options['max_retries']=0
        if options:client=client.with_options(**options)
        metering.check_cancelled()
        response=client.chat.completions.create(**kwargs)
        usage=getattr(response,'usage',None)
        metering.check_cancelled()
        return response
    finally:metering.settle_call(call,usage)


def _start_stream(kwargs):
    call=metering.reserve_call(kwargs,'answer')
    try:
        client=_get_client()
        if call:client=client.with_options(max_retries=0)
        metering.check_cancelled()
        return client.chat.completions.create(**kwargs),call
    except BaseException:
        metering.settle_call(call,None)
        raise


def stream_completion(
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> Iterator[dict]:
    """流式补全。

    产出事件:
      {"type": "delta", "content": str}              文本增量
      {"type": "tool_calls", "tool_calls": [...]}    流结束时模型决定调用工具
      {"type": "usage", "usage": {...}}              本次调用的用量(成本核算用)
      {"type": "completion", ...}                    内部结束状态与工具回传信息
    """
    kwargs: dict[str, Any] = {
        "model": settings.deepseek_model,
        "messages": messages,
        "stream": True,
        # 开启后流的最后一个 chunk 才会带 usage;否则拿不到 token 用量。
        "stream_options": {"include_usage": True},
        "reasoning_effort": reasoning_effort or settings.llm_reasoning_effort,
    }
    if tools:
        kwargs["tools"] = tools
    if temperature is not None:
        kwargs["temperature"] = temperature
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens

    try:
        response,call = _start_stream(kwargs)
    except Exception as exc:  # noqa: BLE001
        # 少数兼容端点不认 stream_options。宁可丢用量统计,也不能让聊天整体不可用。
        # 只在这个参数确实被拒绝时重试,避免把鉴权失败也重发一遍。
        text = str(exc).lower()
        if "stream_options" not in text and not isinstance(exc, TypeError):
            raise
        logger.warning("端点不接受 stream_options,退回不带用量的调用")
        kwargs.pop("stream_options")
        response,call = _start_stream(kwargs)

    tool_calls: dict[int, dict] = {}
    usage: dict | None = None
    finish_reason = None
    reasoning = []

    try:
        with metering.closing_response(response):
            for chunk in response:
                # 用量块的 choices 是空的,必须在下面的判空之前取,否则会被 continue 跳过。
                if getattr(chunk, "usage", None):
                    usage = chunk.usage.model_dump()
                metering.heartbeat()

                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if chunk.choices[0].finish_reason is not None:
                    finish_reason = chunk.choices[0].finish_reason
                if delta and getattr(delta, "reasoning_content", None):
                    reasoning.append(delta.reasoning_content)
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
            metering.check_cancelled()
    finally:
        metering.settle_call(call,usage)

    if tool_calls:
        yield {"type": "tool_calls", "tool_calls": [tool_calls[i] for i in sorted(tool_calls)]}
    if usage:
        yield {"type": "usage", "usage": usage}
    # Internal only: preserve reasoning for tool round trips, never send it to the UI.
    yield {"type": "completion", "finish_reason": finish_reason,
           "reasoning_content": "".join(reasoning)}
