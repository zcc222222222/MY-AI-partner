"""FastAPI 入口。"""
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
