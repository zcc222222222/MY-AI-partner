"""工具注册表:function calling 的 schema 与处理器。

v1.0 只放了两个不依赖知识库的小工具,用来打通「Agent 编排器 → 工具」链路。
RAG 阶段的 retrieve_knowledge / get_team_build 等工具直接在这里追加即可。
"""
import json
import random
from datetime import datetime
from typing import Any, Callable


def _get_current_time() -> dict:
    now = datetime.now()
    return {
        "time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "weekday": "一二三四五六日"[now.weekday()],
    }


def _roll_dice(sides: int = 6, count: int = 1) -> dict:
    sides = max(2, min(int(sides), 100))
    count = max(1, min(int(count), 10))
    rolls = [random.randint(1, sides) for _ in range(count)]
    return {"rolls": rolls, "total": sum(rolls), "sides": sides}


TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前的日期和时间。当用户问到现在几点、今天几号、今天星期几时使用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "roll_dice",
            "description": "掷骰子。当用户想掷骰子、抽签、做随机决定时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "sides": {"type": "integer", "description": "骰子面数,默认 6"},
                    "count": {"type": "integer", "description": "骰子数量,默认 1"},
                },
                "required": [],
            },
        },
    },
]

_HANDLERS: dict[str, Callable[..., dict]] = {
    "get_current_time": lambda **_kw: _get_current_time(),
    "roll_dice": lambda **kw: _roll_dice(**kw),
}


def execute_tool(name: str, arguments: str) -> str:
    """执行工具并以 JSON 字符串返回结果(喂回给模型)。"""
    handler = _HANDLERS.get(name)
    if handler is None:
        return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)
    try:
        kwargs = json.loads(arguments) if arguments else {}
        return json.dumps(handler(**kwargs), ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
