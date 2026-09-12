"""聚合所有路由。"""
from fastapi import APIRouter

from .characters import router as characters_router
from .chat import router as chat_router
from .health import router as health_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(characters_router)
api_router.include_router(chat_router)
