"""Persistent, one-use registration challenges, HMAC at rest and atomic limits."""
from datetime import timedelta
import hashlib
import hmac
import secrets
from fastapi import HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from .. import models
from ..config import settings
from . import mail_service


def digest(email, nonce, code):
    if len(settings.email_code_secret) < 32:
        raise HTTPException(status_code=503, detail="邮件服务尚未配置，请联系管理员")
    return hmac.new(settings.email_code_secret.encode(), f"register:{email}:{nonce}:{code}".encode(), hashlib.sha256).hexdigest()


def request_code(db, email):
    mail_service.require_mail()
    now = models.utcnow()
    code, nonce = f"{secrets.randbelow(1000000):06d}", secrets.token_hex(16)
    values = dict(nonce=nonce, code_hash=digest(email, nonce, code), expires_at=now + timedelta(minutes=10),
                  resend_after=now + timedelta(seconds=60), attempts=0, delivered=False)
    # Insert a tombstone if absent; then compare-and-swap the cooldown to reserve
    # the address before any external SMTP call. No transaction held during SMTP.
    try:
        with db.begin_nested():
            db.add(models.EmailVerification(email=email, **{**values, "resend_after": now - timedelta(seconds=1)}))
            db.flush()
    except IntegrityError:
        pass
    changed = db.execute(update(models.EmailVerification).where(models.EmailVerification.email == email,
        models.EmailVerification.resend_after <= now).values(**values)).rowcount
    db.commit()
    if not changed:
        raise HTTPException(status_code=429, detail="验证码发送过于频繁，请60秒后重试", headers={"Retry-After": "60"})
    # Registered addresses receive the same public acknowledgement but no mail.
    if db.scalar(select(models.User.id).where(models.User.email == email)):
        db.rollback()
        return
    db.rollback()
    mail_service.send_registration_code(email, code)
    db.execute(update(models.EmailVerification).where(models.EmailVerification.email == email,
        models.EmailVerification.nonce == nonce).values(delivered=True))
    db.commit()


def check_code(db, email, code):
    now = models.utcnow()
    changed = db.execute(update(models.EmailVerification).where(models.EmailVerification.email == email,
        models.EmailVerification.expires_at > now, models.EmailVerification.delivered.is_(True),
        models.EmailVerification.attempts < 5).values(attempts=models.EmailVerification.attempts + 1)).rowcount
    # Incorrect attempts must survive registration rollback.
    db.commit()
    row = db.scalar(select(models.EmailVerification).where(models.EmailVerification.email == email))
    if not changed or row is None or not hmac.compare_digest(row.code_hash, digest(email, row.nonce, code)):
        raise HTTPException(status_code=400, detail="验证码错误、已失效或尝试次数过多，请重新获取")
    return row.nonce


def consume_code(db, email, nonce):
    changed = db.execute(delete(models.EmailVerification).where(models.EmailVerification.email == email,
        models.EmailVerification.nonce == nonce, models.EmailVerification.delivered.is_(True),
        models.EmailVerification.attempts <= 5, models.EmailVerification.expires_at > models.utcnow())).rowcount
    if changed != 1:
        raise HTTPException(status_code=400, detail="验证码已失效，请重新获取")
    # Commit together with the new account, never consume a code on duplicate user.
