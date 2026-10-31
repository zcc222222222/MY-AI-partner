"""Argon2 passwords and revocable opaque login sessions. No JWT or raw token storage."""
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import hmac
import secrets

from fastapi import HTTPException
from pwdlib import PasswordHash
from sqlalchemy import case, delete, or_, select, update
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
    is_guest: bool = False


def active_identity():
    return or_(models.User.is_guest.is_(False), models.User.guest_expires_at > models.utcnow())


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
    # Serialize session issuance with password resets, including on SQLite.
    # A login that verified an old password cannot create a session after reset.
    changed = db.execute(update(models.User).where(models.User.id == user.id,
        models.User.password_hash == user.password_hash, models.User.is_active.is_(True))
        .values(password_hash=models.User.password_hash), execution_options={"synchronize_session": False}).rowcount
    if changed != 1:
        db.rollback()
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if user.is_guest and (not settings.guest_enabled or not user.guest_expires_at or user.guest_expires_at <= models.utcnow()):
        db.rollback()
        raise HTTPException(status_code=401, detail="体验已结束，请注册或登录")
    raw = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    db.add(models.AuthSession(token_hash=token_digest(raw), user_id=user.id, csrf_token=csrf,
                              expires_at=min(models.utcnow() + timedelta(hours=settings.auth_session_hours),
                                  user.guest_expires_at) if user.is_guest else models.utcnow() + timedelta(hours=settings.auth_session_hours)))
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
    valid = passwords.verify(password, user.password_hash if user and not user.is_guest else _DUMMY_HASH)
    if not valid or user is None or not user.is_active or user.is_guest:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    raw, csrf = issue_session(db, user)
    return user, raw, csrf


def authenticate(db: Session, raw: str | None):
    if not raw or len(raw) > 128:
        raise HTTPException(status_code=401, detail="请先登录")
    digest = token_digest(raw)
    row = db.execute(select(models.AuthSession, models.User).join(models.User, models.User.id == models.AuthSession.user_id)
                     .where(models.AuthSession.token_hash == digest, models.AuthSession.expires_at > models.utcnow(), models.User.is_active.is_(True), active_identity())).first()
    if not row or row[1].is_guest and not settings.guest_enabled:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    return Principal(row[1].id, digest, row[0].csrf_token, row[1].is_guest)


def validate_principal(db: Session, principal: Principal):
    found = db.scalar(select(models.AuthSession.token_hash).join(models.User, models.User.id == models.AuthSession.user_id)
                      .where(models.AuthSession.token_hash == principal.token_hash, models.AuthSession.user_id == principal.user_id,
                             models.AuthSession.expires_at > models.utcnow(), models.User.is_active.is_(True)))
    user = db.get(models.User, principal.user_id)
    if user and user.is_guest and (not settings.guest_enabled or not user.guest_expires_at or user.guest_expires_at <= models.utcnow()):
        found = None
    if not found:
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")


def check_csrf(principal: Principal, token: str | None):
    if not token or not hmac.compare_digest(principal.csrf_token, token):
        raise HTTPException(status_code=403, detail="请求校验失败，请刷新页面重试")
