"""Agent 编排器:轻量 function-calling 循环。

流程:流式请求模型 → 若模型返回 tool_calls,则执行工具并把结果喂回 →
继续流式输出最终回答。最多执行 MAX_TOOL_ROUNDS 轮。

预算耗尽时**不会直接中断**,而是追加一条收口提示、去掉 tools 再请求一次 ——
否则模型只吐 tool_calls 不吐正文时,会落库一条空回复。
"""
from typing import Any, Iterator

from ..llm import stream_completion
from .tools import execute_tool

MAX_TOOL_ROUNDS = 2

WRAP_UP_PROMPT = (
    "工具调用次数已达上限。请基于已经获得的信息直接给出最终回答,"
    "不要再请求调用工具;如果信息不足,就如实说明还缺什么。"
)


def run_agent(
    messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> Iterator[dict]:
    """流式产出事件:{"type":"delta"} / {"type":"usage"}。

    调用方把增量拼接成完整回答;持久化由调用方负责。
    """
    convo = list(messages)
    tool_rounds = 0

    def stream(with_tools: list[dict] | None) -> Iterator[dict]:
        return stream_completion(
            convo,
            tools=with_tools,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    while True:
        tool_calls = None
        for event in stream(tools):
            if event["type"] == "delta":
                yield event
            elif event["type"] == "tool_calls":
                tool_calls = event["tool_calls"]
            elif event["type"] == "usage":
                yield event  # 透传,由上层汇总记录

        if not tool_calls:
            return

        if tool_rounds >= MAX_TOOL_ROUNDS:
            # 收口:不带 tools,模型不会再产出 tool_calls,只会给最终正文。
            #
            # 这里用 user 而不是 system —— 收口提示出现在对话中段,
            # 各家对「中段 system」的容忍度不一致,user 是所有实现都接受的。
            convo.append({"role": "user", "content": WRAP_UP_PROMPT})
            for event in stream(None):
                if event["type"] == "delta":
                    yield event
                elif event["type"] == "usage":
                    yield event
            return

        tool_rounds += 1
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": None}
        assistant_msg["tool_calls"] = tool_calls
        convo.append(assistant_msg)

        for tc in tool_calls:
            result = execute_tool(tc["function"]["name"], tc["function"]["arguments"])
            convo.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
