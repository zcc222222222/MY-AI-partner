"""Short-lived HTTP authentication probes; no DB connection held by dependencies."""
from fastapi import Depends, HTTPException, Request
from .config import settings
from .database import SessionLocal
from .services.auth_service import authenticate, check_csrf, Principal


def check_origin(request: Request):
    origin = request.headers.get("origin")
    same = f"{request.url.scheme}://{request.url.netloc}"
    if origin and origin not in {*settings.cors_origins, same}:
        raise HTTPException(status_code=403, detail="不允许的请求来源")


def current_user(request: Request) -> Principal:
    with SessionLocal() as db:
        principal = authenticate(db, request.cookies.get(settings.auth_cookie_name))
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        check_origin(request)
        check_csrf(principal, request.headers.get("x-csrf-token"))
    return principal


def knowledge_admin(principal: Principal = Depends(current_user)) -> Principal:
    if principal.user_id not in settings.knowledge_admin_user_ids:
        raise HTTPException(status_code=403, detail='此账号没有知识库管理权限')
    return principal
