"""FastAPI 入口。"""
import logging
import asyncio
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
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
    from .migrations import migrate
    migrate(engine)
    from .services import usage_service
    from .services import guest_service
    with SessionLocal() as db,db.begin():
        usage_service.lock(db)
        usage_service.expire(db)
        guest_service.cleanup(db)
    with SessionLocal() as db:
        seed_characters(db)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _bootstrap()
    if not settings.api_key_ready:
        # 宁可起不来,也不要让用户在聊天时才收到一个被 SSE 包起来的鉴权错误。
        raise RuntimeError(
            "未设置有效的 DEEPSEEK_API_KEY,拒绝启动。"
            "请在 backend/.env 里填入真实密钥(不要保留 .env.example 的占位值)。"
        )
    logger.info(
        "LLM 配置就绪 model=%s base_url=%s temperature=%s max_tokens=%s",
        settings.deepseek_model,
        settings.deepseek_base_url,
        settings.llm_temperature,
        settings.llm_max_tokens,
    )
    if not settings.mail_ready:
        logger.warning("邮件服务尚未配置：已有账号可登录，新账号需配置 SMTP 后验证邮箱注册")
    cleanup_task = asyncio.create_task(_guest_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        from .agents.live_guides import CLIENT
        CLIENT.close()


async def _guest_cleanup_loop():
    def clean():
        from .services import usage_service, guest_service
        with SessionLocal() as db, db.begin():
            usage_service.lock(db)
            usage_service.expire(db)
            guest_service.cleanup(db)
    while True:
        await asyncio.sleep(3600)
        try:
            await asyncio.to_thread(clean)
        except Exception:
            logger.exception('guest_cleanup_failed')


app = FastAPI(title="爱弥斯 AI 助手", version="1.0.0", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def redacted_validation_error(request, exc):
    # Pydantic's default response echoes invalid input, including submitted passwords.
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]} for error in exc.errors()]})

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.middleware("http")
async def private_api_cache(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith(("/api/v1/auth/", "/api/v1/chat/", "/api/v1/knowledge/ops", "/api/v1/usage")):
        response.headers["Cache-Control"] = "no-store"
    return response

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

# ---------------------------------------------------------------------------
# 静态产物的缓存策略。
#
# 默认的 StaticFiles 只发 ETag / Last-Modified,**不发 Cache-Control** —— 浏览器
# 于是按「启发式缓存」自己算新鲜度(通常取「距今时间的 10%」),这段时间内连条件
# 请求都不发。后果很具体:重新构建后浏览器还在用旧的 index.html,而旧外壳里没有
# 的东西(例如 favicon 的 <link>)它就永远看不到 —— 「换了图标页签上一直没有」
# 和「重新 build 了页面还是旧的」是同一个病。
#
# 带内容哈希的构建产物文件名不会变,可以放心长期缓存;其余一律 no-cache,
# 语义是「可以缓存,但每次都要回源校验」—— 命中就是 304,代价极小,
# 却保证替换素材(图标、立绘、背景视频)之后一定能生效。
# ---------------------------------------------------------------------------
_HASHED_ASSET = re.compile(r"^assets/.+-[A-Za-z0-9_-]{8}\.(?:js|css)$")


class SpaStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        if "cache-control" not in response.headers:
            # Starlette 在 Windows 上会先把路径规范化成反斜杠(os.path.normpath),
            # 只按 "/" 写正则的话这里会静默走成 no-cache —— 不算坏,但不是本意。
            normalized = path.replace("\\", "/")
            response.headers["cache-control"] = (
                "public, max-age=31536000, immutable"
                if _HASHED_ASSET.match(normalized)
                else "no-cache"
            )
        return response


if FRONTEND_DIST.is_dir():
    # html=True:访问 / 时返回目录下的 index.html
    app.mount("/", SpaStaticFiles(directory=FRONTEND_DIST, html=True), name="spa")
    logger.info("已挂载前端构建产物,单命令启动就绪: %s", FRONTEND_DIST)
else:
    logger.info(
        "未找到前端构建产物(%s),本次只提供 API。"
        "要单命令启动,先在 frontend 目录执行一次 npm run build。",
        FRONTEND_DIST,
    )
