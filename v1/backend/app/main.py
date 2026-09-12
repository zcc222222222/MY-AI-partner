"""FastAPI 入口。"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.router import api_router
from .config import settings
from .database import Base, SessionLocal, engine
from .seed import seed_characters


def _bootstrap() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_characters(db)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _bootstrap()
    if not settings.deepseek_api_key:
        print("[警告] 未设置 DEEPSEEK_API_KEY,聊天接口会失败。请在 backend/.env 中配置。")
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
