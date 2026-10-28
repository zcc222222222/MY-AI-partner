"""健康检查。"""
from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/api/v1/health")
def health():
    return {"status": "ok"}
