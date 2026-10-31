"""Isolated expiring visitors with opaque cookies and shared anonymous IP quotas."""
from datetime import timedelta, timezone
import hashlib
import hmac
import ipaddress
import uuid

from fastapi import HTTPException
from sqlalchemy import delete, exists, select, update
from .. import models
from ..config import settings
from . import auth_service, usage_service as usage


def ip_hash(remote):
    if len(settings.guest_ip_secret) < 32:
        raise HTTPException(503, "免注册体验尚未配置，请注册或登录")
    try:
        address = ipaddress.ip_address(remote)
        if address.version == 6 and address.ipv4_mapped:
            address = address.ipv4_mapped
        # IPv6 privacy-address rotation within one subnet must not mint more quota.
        remote = str(ipaddress.ip_network(f'{address}/64', strict=False)) if address.version == 6 else str(address)
    except ValueError:
        remote = 'unknown'
    return hmac.new(settings.guest_ip_secret.encode(), ('guest-ip:' + remote).encode(), hashlib.sha256).hexdigest()


def ip_bucket(identity, moment):
    day = moment.replace(tzinfo=timezone.utc).astimezone(usage.LOCAL).strftime('%Y-%m-%d')
    return f'guest-ip:{identity}:day:{day}'


def cleanup(db, moment=None):
    """Bounded purge; retain pseudonymous users and billing ledger for reconciliation."""
    moment = moment or models.utcnow()
    running = exists(select(models.GenerationRequest.id).where(
        models.GenerationRequest.owner_id == models.User.id, models.GenerationRequest.status.in_(usage.ACTIVE)))
    ids = list(db.scalars(select(models.User.id).where(models.User.is_guest.is_(True),
        models.User.is_active.is_(True), models.User.guest_expires_at <= moment, ~running).limit(100)))
    if not ids:
        return 0
    sessions = select(models.ChatSession.id).where(models.ChatSession.owner_id.in_(ids))
    db.execute(delete(models.ContextSummary).where(models.ContextSummary.owner_id.in_(ids)))
    db.execute(delete(models.Message).where(models.Message.session_id.in_(sessions)))
    db.execute(delete(models.ChatSession).where(models.ChatSession.owner_id.in_(ids)))
    db.execute(delete(models.AuthSession).where(models.AuthSession.user_id.in_(ids)))
    db.execute(update(models.User).where(models.User.id.in_(ids)).values(is_active=False, guest_ip_hash=None))
    return len(ids)


def enter(db, raw, remote):
    if raw:
        try:
            principal = auth_service.authenticate(db, raw)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
        else:
            # Repeated entry preserves an existing guest or registered account.
            return db.get(models.User, principal.user_id), raw, principal.csrf_token, False
    if not settings.guest_enabled:
        raise HTTPException(503, "免注册体验暂未开放，请注册或登录")
    identity = ip_hash(remote)
    auth_service.throttle(db, remote, 'guest-start-ip', limit=10, minutes=60)
    usage.lock(db)
    usage.expire(db)
    moment = usage.now()
    cleanup(db, moment)
    quota = usage.bucket(db, ip_bucket(identity, moment))
    if quota.questions >= settings.guest_daily_questions:
        raise usage.QuotaError('该网络今日免注册体验额度已用完，请注册或登录继续', retry=usage._reset_seconds(moment))
    day = usage.keys(0, moment)[0].rsplit(':', 1)[1]
    starts = usage.bucket(db, f'guest-starts:day:{day}')
    if starts.questions >= settings.guest_daily_starts:
        raise usage.QuotaError('今日免注册体验名额已满，请注册或登录', retry=usage._reset_seconds(moment))
    starts.questions += 1
    user = models.User(username='guest_' + uuid.uuid4().hex[:24], display_name='体验访客',
        password_hash='!guest-no-password', is_guest=True, guest_ip_hash=identity,
        guest_expires_at=moment + timedelta(hours=settings.guest_ttl_hours))
    db.add(user)
    db.flush()
    raw, csrf = auth_service.issue_session(db, user)
    return user, raw, csrf, True
