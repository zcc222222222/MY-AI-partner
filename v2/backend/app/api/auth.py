"""Registration, login, current user and logout; cookies carry opaque credentials."""
from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from sqlalchemy import delete

from .. import models, schemas
from ..auth import current_user, check_origin
from ..config import settings
from ..database import SessionLocal
from ..services import auth_service, email_verification, password_reset

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def set_cookie(response: Response, raw):
    response.set_cookie(settings.auth_cookie_name, raw, max_age=settings.auth_session_hours * 3600,
                        httponly=True, secure=settings.auth_cookie_secure, samesite="lax", path="/")
    response.headers["Cache-Control"] = "no-store"


def _signin(body, request, response, registering=False):
    check_origin(request)
    with SessionLocal() as db:
        auth_service.throttle(db, request.client.host if request.client else "unknown", "credentials")
        if registering:
            user, raw, csrf = auth_service.register(db, body.username, body.password.get_secret_value(), body.display_name,
                email=str(body.email), email_code=body.email_code)
        else:
            user, raw, csrf = auth_service.login(db, body.username, body.password.get_secret_value())
        old = request.cookies.get(settings.auth_cookie_name)
        if old and len(old) <= 128:
            db.execute(delete(models.AuthSession).where(models.AuthSession.token_hash == auth_service.token_digest(old)))
            db.commit()
        payload = schemas.AuthOut(user=schemas.UserOut.model_validate(user), csrf_token=csrf)
    set_cookie(response, raw)
    return payload


@router.post("/email-code")
def send_email_code(body: schemas.EmailCodeRequest, request: Request):
    check_origin(request)
    with SessionLocal() as db:
        remote = request.client.host if request.client else "unknown"
        auth_service.throttle(db, remote, "email-code-ip", limit=10, minutes=60)
        auth_service.throttle(db, str(body.email), "email-code-address", limit=5, minutes=60)
        email_verification.request_code(db, str(body.email))
    return {"message": "若该邮箱可用于注册，验证码将发送到邮箱，请检查收件箱及垃圾邮件", "retry_after": 60, "expires_in": 600}


@router.post("/register", response_model=schemas.AuthOut, status_code=201)
def register(body: schemas.RegisterIn, request: Request, response: Response):
    return _signin(body, request, response, registering=True)


@router.post("/password-reset/code")
def request_password_reset(body: schemas.EmailCodeRequest, request: Request, background_tasks: BackgroundTasks):
    check_origin(request)
    with SessionLocal() as db:
        remote = request.client.host if request.client else "unknown"
        # Share email send limits with registration to prevent cross-purpose spam.
        auth_service.throttle(db, remote, "email-code-ip", limit=10, minutes=60)
        auth_service.throttle(db, str(body.email), "email-code-address", limit=5, minutes=60)
        delivery = password_reset.request_code(db, str(body.email))
    if delivery:
        background_tasks.add_task(password_reset.deliver_code, *delivery)
    return {"message": "若该邮箱已绑定可恢复的账号，重置验证码将发送到邮箱，请检查收件箱及垃圾邮件", "retry_after": 60, "expires_in": 600}


@router.post("/password-reset")
def reset_password(body: schemas.PasswordResetIn, request: Request, response: Response):
    check_origin(request)
    with SessionLocal() as db:
        remote = request.client.host if request.client else "unknown"
        auth_service.throttle(db, remote, "password-reset-ip", limit=20, minutes=60)
        auth_service.throttle(db, str(body.email), "password-reset-address", limit=10, minutes=60)
        password_reset.reset_password(db, str(body.email), body.email_code, body.new_password.get_secret_value())
    # Recovery never signs in automatically. All old tokens were revoked atomically.
    response.delete_cookie(settings.auth_cookie_name, path="/", secure=settings.auth_cookie_secure, httponly=True, samesite="lax")
    return {"message": "密码已重置，请使用用户名和新密码重新登录"}


@router.post("/login", response_model=schemas.AuthOut)
def login(body: schemas.LoginIn, request: Request, response: Response):
    return _signin(body, request, response)


@router.get("/me", response_model=schemas.AuthOut)
def me(response: Response, principal: auth_service.Principal = Depends(current_user)):
    response.headers["Cache-Control"] = "no-store"
    with SessionLocal() as db:
        user = db.get(models.User, principal.user_id)
        return schemas.AuthOut(user=schemas.UserOut.model_validate(user), csrf_token=principal.csrf_token)


@router.post("/logout")
def logout(response: Response, principal: auth_service.Principal = Depends(current_user)):
    with SessionLocal() as db:
        db.execute(delete(models.AuthSession).where(models.AuthSession.token_hash == principal.token_hash))
        db.commit()
    response.delete_cookie(settings.auth_cookie_name, path="/", secure=settings.auth_cookie_secure, httponly=True, samesite="lax")
    response.headers["Cache-Control"] = "no-store"
    return {"ok": True}
