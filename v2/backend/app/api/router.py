"""聚合所有路由。"""
from fastapi import APIRouter

from .characters import router as characters_router
from .chat import router as chat_router
from .health import router as health_router
from .auth import router as auth_router
from .knowledge_images import router as knowledge_images_router
from .knowledge_ops import router as knowledge_ops_router
from .usage import router as usage_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(auth_router)
api_router.include_router(characters_router)
api_router.include_router(chat_router)
api_router.include_router(knowledge_images_router)
api_router.include_router(knowledge_ops_router)
api_router.include_router(usage_router)
