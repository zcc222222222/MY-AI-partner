"""工具注册表:function calling 的 schema 与处理器。

内置的两个小工具(get_current_time / roll_dice)用来打通「编排器 → 工具」链路。
知识库工具在下面以可插拔方式并入 —— 追加新工具只需要改这里,编排器、路由、
前端都不用动。
"""
import json
import random
from .live_guides import TOOLS as _LIVE_TOOLS, HANDLERS as _LIVE_HANDLERS
from datetime import datetime
from typing import Any, Callable

# ---------------------------------------------------------------------------
# 知识库工具(确定性 SQL 通道:不经过检索、不经过 LLM,所以不会幻觉)。
#
# 用 try/except 包住 import 是一个刻意的降级设计:知识库包不在 sys.path 上
# (例如把 backend/ 单独拷出去跑)时,后端必须照常启动 —— 少四个工具,
# 远好过整个服务起不来。知识库**没构建**的情况在工具内部处理(返回 error JSON),
# 连 import 都不会失败。
#
# 启动方式决定了知识库能否被导入:必须在 backend/ 目录下启动 uvicorn,
# 那时 sys.path[0] 就是 backend/,`knowledge` 才是一个可导入的顶层包。
# ---------------------------------------------------------------------------
try:  # pragma: no cover - 取决于部署方式
    from knowledge.tools import HANDLERS as _KNOWLEDGE_HANDLERS
    from knowledge.tools import TOOLS as _KNOWLEDGE_TOOLS
except ImportError:  # noqa: BLE001
    _KNOWLEDGE_TOOLS: list[dict] = []
    _KNOWLEDGE_HANDLERS: dict = {}

try:
    from llmwiki.tools import TOOLS as _WIKI_TOOLS, HANDLERS as _WIKI_HANDLERS
except ImportError:
    _WIKI_TOOLS, _WIKI_HANDLERS = [], {}


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

# 并入知识库工具。两边的处理器契约一致(都返回 dict,序列化各自负责),
# 所以直接 update 即可,不需要适配层。
TOOLS = [*TOOLS, *_KNOWLEDGE_TOOLS, *_WIKI_TOOLS, *_LIVE_TOOLS]
_HANDLERS.update(_KNOWLEDGE_HANDLERS)
_HANDLERS.update(_WIKI_HANDLERS)
_HANDLERS.update(_LIVE_HANDLERS)


def execute_tool(name: str, arguments: str) -> str:
    """执行工具并以 JSON 字符串返回结果(喂回给模型)。

    契约:永不抛异常。未知工具、参数坏掉、知识库没建 —— 一律走 JSON 里的
    error 字段。调用方是 function-calling 循环,抛异常会把一次聊天变成 500,
    而一条 error 会被模型读进去并自我纠正。
    """
    handler = _HANDLERS.get(name)
    if handler is None:
        return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)
    try:
        kwargs = json.loads(arguments) if arguments else {}
        if name not in ('get_current_time','roll_dice'):
            from ..services.retrieval_diagnostics import run
            result = run(name,kwargs,lambda:handler(**kwargs))
        else: result = handler(**kwargs)
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
