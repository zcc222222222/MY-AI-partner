"""Agent 编排器:轻量 function-calling 循环。

流程:流式请求模型 → 若模型返回 tool_calls,则执行工具并把结果喂回 →
继续流式输出最终回答。最多执行 MAX_TOOL_ROUNDS 轮,避免死循环。
"""
from typing import Any, Iterator

from ..llm import stream_completion
from .tools import execute_tool

MAX_TOOL_ROUNDS = 2


def run_agent(messages: list[dict], tools: list[dict] | None = None) -> Iterator[dict]:
    """流式产出 {"type": "delta", "content": str} 事件。

    调用方把增量拼接成完整回答;持久化由调用方负责。
    """
    convo = list(messages)
    tool_rounds = 0

    while True:
        tool_calls = None
        for event in stream_completion(convo, tools=tools):
            if event["type"] == "delta":
                yield event
            elif event["type"] == "tool_calls":
                tool_calls = event["tool_calls"]

        if not tool_calls or tool_rounds >= MAX_TOOL_ROUNDS:
            return

        tool_rounds += 1
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": None}
        assistant_msg["tool_calls"] = tool_calls
        convo.append(assistant_msg)

        for tc in tool_calls:
            result = execute_tool(tc["function"]["name"], tc["function"]["arguments"])
            convo.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
