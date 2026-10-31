"""Purpose-bound email challenges; password update and session revocation are atomic."""
from datetime import timedelta
import hashlib
import hmac
import logging
import secrets

from fastapi import HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from .. import models, schemas
from ..config import settings
from ..database import SessionLocal
from . import auth_service, mail_service

logger = logging.getLogger(__name__)
INVALID_CODE = "验证码错误、已失效或尝试次数过多，请重新获取"


def digest(email, nonce, code):
    if len(settings.email_code_secret) < 32:
        raise HTTPException(status_code=503, detail="邮件服务尚未配置，请联系管理员")
    return hmac.new(settings.email_code_secret.encode(),
        f"password-reset:{email}:{nonce}:{code}".encode(), hashlib.sha256).hexdigest()


def request_code(db, email):
    mail_service.require_mail()
    now = models.utcnow()
    user = db.scalar(select(models.User).where(models.User.email == email,
        models.User.email_verified_at.is_not(None), models.User.is_active.is_(True)))
    code, nonce = f"{secrets.randbelow(1000000):06d}", secrets.token_hex(16)
    values = dict(user_id=user.id if user else None,
        credential_digest=auth_service.token_digest(user.password_hash) if user else None,
        nonce=nonce, code_hash=digest(email, nonce, code), created_at=now,
        expires_at=now + timedelta(minutes=10), resend_after=now + timedelta(seconds=60),
        attempts=0, delivered=False)
    try:
        with db.begin_nested():
            db.add(models.PasswordResetChallenge(email=email,
                **{**values, 'resend_after': now - timedelta(seconds=1)}))
            db.flush()
    except IntegrityError:
        pass
    changed = db.execute(update(models.PasswordResetChallenge).where(
        models.PasswordResetChallenge.email == email,
        models.PasswordResetChallenge.resend_after <= now).values(**values)).rowcount
    db.commit()
    if not changed:
        raise HTTPException(status_code=429, detail="验证码发送过于频繁，请60秒后重试", headers={"Retry-After": "60"})
    return (email, nonce, code) if user else None


def deliver_code(email, nonce, code):
    # SMTP runs after the HTTP acknowledgement, without holding a transaction.
    # Never let transport failures expose whether an address belongs to a user.
    try:
        with SessionLocal() as db:
            row = db.get(models.PasswordResetChallenge, email)
            current = bool(row and row.nonce == nonce and row.expires_at > models.utcnow())
        if not current:
            return
        mail_service.send_password_reset_code(email, code)
        with SessionLocal() as db:
            db.execute(update(models.PasswordResetChallenge).where(
                models.PasswordResetChallenge.email == email,
                models.PasswordResetChallenge.nonce == nonce).values(delivered=True))
            db.commit()
    except Exception:
        # No email address, credentials, verification code or exception payload.
        logger.warning("password_reset_delivery_failed")


def reset_password(db, email, code, new_password):
    now = models.utcnow()
    changed = db.execute(update(models.PasswordResetChallenge).where(
        models.PasswordResetChallenge.email == email,
        models.PasswordResetChallenge.expires_at > now,
        models.PasswordResetChallenge.delivered.is_(True),
        models.PasswordResetChallenge.attempts < 5).values(
            attempts=models.PasswordResetChallenge.attempts + 1)).rowcount
    db.commit()  # Failed guesses survive later rollback.
    row = db.get(models.PasswordResetChallenge, email)
    if not changed or row is None or not hmac.compare_digest(row.code_hash, digest(email, row.nonce, code)):
        raise HTTPException(status_code=400, detail=INVALID_CODE)
    user = db.get(models.User, row.user_id) if row.user_id else None
    if (not user or not user.is_active or user.email != email or not user.email_verified_at
            or auth_service.token_digest(user.password_hash) != row.credential_digest):
        raise HTTPException(status_code=400, detail=INVALID_CODE)
    try:
        schemas.validate_new_password(new_password, new_password, user.username)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    password_hash = auth_service.passwords.hash(new_password)
    try:
        consumed = db.execute(delete(models.PasswordResetChallenge).where(
            models.PasswordResetChallenge.email == email,
            models.PasswordResetChallenge.nonce == row.nonce,
            models.PasswordResetChallenge.delivered.is_(True),
            models.PasswordResetChallenge.attempts <= 5,
            models.PasswordResetChallenge.expires_at > models.utcnow())).rowcount
        changed = db.execute(update(models.User).where(models.User.id == user.id,
            models.User.password_hash == user.password_hash, models.User.email == email,
            models.User.email_verified_at.is_not(None), models.User.is_active.is_(True))
            .values(password_hash=password_hash, password_changed_at=models.utcnow()),
            execution_options={"synchronize_session": False}).rowcount
        if consumed != 1 or changed != 1:
            raise HTTPException(status_code=400, detail=INVALID_CODE)
        db.execute(delete(models.AuthSession).where(models.AuthSession.user_id == user.id))
        db.commit()
    except Exception:
        db.rollback()
        raise
