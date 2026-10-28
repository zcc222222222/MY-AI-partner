"""ORM 模型:角色卡、会话、消息。"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, BigInteger, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.mysql import LONGTEXT

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _session_id() -> str:
    return uuid.uuid4().hex


class Character(Base):
    __tablename__ = "characters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(64))
    version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    avatar: Mapped[str] = mapped_column(String(255), default="")
    greeting: Mapped[str] = mapped_column(Text, default="")
    system_prompt: Mapped[str] = mapped_column(Text)
    personality: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_session_id)
    character_id: Mapped[int] = mapped_column(ForeignKey("characters.id"))
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", name="fk_chat_sessions_owner_id_users", ondelete="SET NULL"), index=True, nullable=True)
    title: Mapped[str] = mapped_column(String(120), default="新会话")
    has_user_message: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    messages: Mapped[list["Message"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    context_summary: Mapped["ContextSummary | None"] = relationship(cascade="all, delete-orphan", uselist=False)


class ContextSummary(Base):
    __tablename__ = "chat_context_summaries"
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id", ondelete="CASCADE"), primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    covered_through: Mapped[int] = mapped_column(Integer)
    payload: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT(), "mysql"))
    format_version: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text().with_variant(LONGTEXT(), "mysql"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    session: Mapped["ChatSession"] = relationship(back_populates="messages")


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(32), unique=True)
    email: Mapped[str | None] = mapped_column(String(254), nullable=True, unique=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    display_name: Mapped[str] = mapped_column(String(64))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class AuthThrottle(Base):
    __tablename__ = "auth_throttles"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer)


class EmailVerification(Base):
    __tablename__ = "email_verifications"
    email: Mapped[str] = mapped_column(String(254), primary_key=True)
    nonce: Mapped[str] = mapped_column(String(32))
    code_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    resend_after: Mapped[datetime] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)


class QuotaBucket(Base):
    __tablename__ = 'quota_buckets'
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    questions: Mapped[int] = mapped_column(Integer, default=0)
    spent_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    reserved_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class GenerationRequest(Base):
    __tablename__ = 'generation_requests'
    __table_args__ = (UniqueConstraint('owner_id','client_request_id',name='uq_generation_owner_client'),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_session_id)
    owner_id: Mapped[int] = mapped_column(ForeignKey('users.id'), index=True)
    session_id: Mapped[str] = mapped_column(String(32), index=True)
    client_request_id: Mapped[str] = mapped_column(String(64))
    input_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), index=True)
    question_buckets: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    lease_until: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    assistant_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)


class LLMUsage(Base):
    __tablename__ = 'llm_usage'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_session_id)
    request_id: Mapped[str] = mapped_column(ForeignKey('generation_requests.id'), index=True)
    purpose: Mapped[str] = mapped_column(String(24))
    model: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(24))
    buckets: Mapped[str] = mapped_column(Text)
    reserved_micros: Mapped[int] = mapped_column(BigInteger)
    charged_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_hit_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price_version: Mapped[str] = mapped_column(String(100))
    input_rate: Mapped[int] = mapped_column(BigInteger)
    cached_rate: Mapped[int] = mapped_column(BigInteger)
    output_rate: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
