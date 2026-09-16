"""FastAPI 入口。"""
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api.router import api_router
from .config import settings
from .database import Base, SessionLocal, engine
from .seed import seed_characters

# uvicorn 只给 uvicorn.* 这几个 logger 配 handler,root 是没有 handler 的 ——
# 结果应用里的 logger.info(...) 会被静默丢掉(lastResort 只放 WARNING 以上)。
# 这一行是「每轮用量日志可见」的前提。
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

# 降噪:httpx 每发一次外呼就打一行 INFO,叠加 logger.exception 的几十行堆栈,
# 会把 chat.completed 那条用量日志彻底淹没 —— 而那条才是加日志的目的。
for _noisy in ("httpx", "httpcore", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def _bootstrap() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_characters(db)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _bootstrap()
    if not settings.api_key_ready:
        # 宁可起不来,也不要让用户在聊天时才收到一个被 SSE 包起来的鉴权错误。
        raise RuntimeError(
            "未设置有效的 DEEPSEEK_API_KEY,拒绝启动。"
            "请在 v1/backend/.env 里填入真实密钥(不要保留 .env.example 的占位值)。"
        )
    logger.info(
        "LLM 配置就绪 model=%s base_url=%s temperature=%s max_tokens=%s",
        settings.deepseek_model,
        settings.deepseek_base_url,
        settings.llm_temperature,
        settings.llm_max_tokens,
    )
    yield


app = FastAPI(title="爱弥斯 AI 助手", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)

# ---------------------------------------------------------------------------
# 单命令启动:把前端构建产物挂到根路径,让一个 uvicorn 进程同时提供 API 和页面。
#
# 三个容易踩的点:
#  1. 必须放在所有 include_router **之后** —— 挂在 "/" 会吃掉它之后注册的全部路由,
#     顺序反了 /api/* 直接 404。
#  2. FastAPI 自带的 /docs、/openapi.json 在建 app 时就注册好了,不受影响。
#  3. dist/ 是构建产物、被 .gitignore 排除,新克隆的仓库里没有 —— 那时跳过挂载,
#     只提供 API(仍可配合 npm run dev 做前后端分离开发),并且明确说清为什么。
# ---------------------------------------------------------------------------
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"

if FRONTEND_DIST.is_dir():
    # html=True:访问 / 时返回目录下的 index.html
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="spa")
    logger.info("已挂载前端构建产物,单命令启动就绪: %s", FRONTEND_DIST)
else:
    logger.info(
        "未找到前端构建产物(%s),本次只提供 API。"
        "要单命令启动,先在 v1/frontend 执行一次 npm run build。",
        FRONTEND_DIST,
    )
