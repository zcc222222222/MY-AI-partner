"""Argon2 passwords and revocable opaque login sessions. No JWT or raw token storage."""
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import hmac
import secrets

from fastapi import HTTPException
from pwdlib import PasswordHash
from sqlalchemy import case, delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from . import email_verification

passwords = PasswordHash.recommended()
# Verify a real hash even when the username does not exist.
_DUMMY_HASH = passwords.hash(secrets.token_urlsafe(32))


@dataclass(frozen=True)
class Principal:
    user_id: int
    token_hash: str
    csrf_token: str


def token_digest(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def throttle(db: Session, remote: str, action: str, *, limit=None, minutes=5):
    key = token_digest(action + ":" + remote)
    now = models.utcnow()
    cutoff = now - timedelta(minutes=minutes)
    try:
        with db.begin_nested():
            db.add(models.AuthThrottle(key=key, window_start=now, attempts=0))
            db.flush()
    except IntegrityError:
        pass
    db.execute(update(models.AuthThrottle).where(models.AuthThrottle.key == key).values(
        attempts=case((models.AuthThrottle.window_start > cutoff, models.AuthThrottle.attempts + 1), else_=1),
        window_start=case((models.AuthThrottle.window_start > cutoff, models.AuthThrottle.window_start), else_=now)))
    attempts = db.scalar(select(models.AuthThrottle.attempts).where(models.AuthThrottle.key == key))
    db.commit()
    if attempts > (settings.auth_rate_limit if limit is None else limit):
        raise HTTPException(status_code=429, detail="操作过于频繁，请稍后再试", headers={"Retry-After": str(minutes * 60)})


def issue_session(db: Session, user: models.User):
    raw = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    db.add(models.AuthSession(token_hash=token_digest(raw), user_id=user.id, csrf_token=csrf,
                              expires_at=models.utcnow() + timedelta(hours=settings.auth_session_hours)))
    # Expired sessions do not need to accumulate forever.
    db.execute(delete(models.AuthSession).where(models.AuthSession.expires_at <= models.utcnow()))
    db.commit()
    return raw, csrf


def register(db: Session, username: str, password: str, display_name=None, *, email: str, email_code: str):
    if db.scalar(select(models.User.id).where(models.User.username == username)):
        raise HTTPException(status_code=409, detail="用户名已被使用")
    if db.scalar(select(models.User.id).where(models.User.email == email)):
        raise HTTPException(status_code=409, detail="邮箱已注册，请直接登录")
    nonce = email_verification.check_code(db, email, email_code)
    user = models.User(username=username, email=email, email_verified_at=models.utcnow(),
                       display_name=display_name or username, password_hash=passwords.hash(password))
    db.add(user)
    try:
        email_verification.consume_code(db, email, nonce)
        db.flush()
        raw, csrf = issue_session(db, user)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="用户名或邮箱已被使用") from None
    return user, raw, csrf


def login(db: Session, username: str, password: str):
    user = db.scalar(select(models.User).where(models.User.username == username))
    valid = passwords.verify(password, user.password_hash if user else _DUMMY_HASH)
    if not valid or user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    raw, csrf = issue_session(db, user)
    return user, raw, csrf


def authenticate(db: Session, raw: str | None):
    if not raw or len(raw) > 128:
        raise HTTPException(status_code=401, detail="请先登录")
    digest = token_digest(raw)
    row = db.execute(select(models.AuthSession, models.User).join(models.User, models.User.id == models.AuthSession.user_id)
                     .where(models.AuthSession.token_hash == digest, models.AuthSession.expires_at > models.utcnow(), models.User.is_active.is_(True))).first()
    if not row:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    return Principal(row[1].id, digest, row[0].csrf_token)


def validate_principal(db: Session, principal: Principal):
    found = db.scalar(select(models.AuthSession.token_hash).join(models.User, models.User.id == models.AuthSession.user_id)
                      .where(models.AuthSession.token_hash == principal.token_hash, models.AuthSession.user_id == principal.user_id,
                             models.AuthSession.expires_at > models.utcnow(), models.User.is_active.is_(True)))
    if not found:
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")


def check_csrf(principal: Principal, token: str | None):
    if not token or not hmac.compare_digest(principal.csrf_token, token):
        raise HTTPException(status_code=403, detail="请求校验失败，请刷新页面重试")
