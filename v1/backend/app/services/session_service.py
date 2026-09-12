"""会话与消息的数据访问,以及标题生成。"""
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .. import models
from ..llm import chat


def list_sessions(db: Session) -> list[models.ChatSession]:
    return list(
        db.scalars(select(models.ChatSession).order_by(desc(models.ChatSession.updated_at)))
    )


def get_session(db: Session, session_id: str) -> models.ChatSession | None:
    return db.get(models.ChatSession, session_id)


def get_default_character(db: Session, character_id: int | None = None) -> models.Character | None:
    if character_id is not None:
        char = db.get(models.Character, character_id)
        if char is not None:
            return char
    return db.scalars(
        select(models.Character).where(models.Character.is_active.is_(True))
    ).first()


def create_session(db: Session, character_id: int | None = None) -> models.ChatSession:
    character = get_default_character(db, character_id)
    if character is None:
        raise RuntimeError("没有可用的角色卡,请先检查 characters/ 目录")
    session = models.ChatSession(character_id=character.id, title="新会话")
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def delete_session(db: Session, session_id: str) -> bool:
    session = db.get(models.ChatSession, session_id)
    if session is None:
        return False
    db.delete(session)
    db.commit()
    return True


def list_messages(db: Session, session_id: str) -> list[models.Message]:
    return list(
        db.scalars(
            select(models.Message)
            .where(models.Message.session_id == session_id)
            .order_by(models.Message.created_at, models.Message.id)
        )
    )


def append_message(db: Session, session_id: str, role: str, content: str) -> models.Message:
    message = models.Message(session_id=session_id, role=role, content=content)
    db.add(message)
    session = db.get(models.ChatSession, session_id)
    if session is not None:
        session.updated_at = models.utcnow()
    db.commit()
    db.refresh(message)
    return message


def generate_title(first_message: str) -> str:
    """首条消息 → 会话标题(复用旧版逻辑)。"""
    try:
        text = chat(
            [
                {
                    "role": "system",
                    "content": "请用3到8个中文字总结以下消息的主题,作为对话标题。只返回标题文字,不要引号,不要标点。",
                },
                {"role": "user", "content": first_message},
            ]
        )
        title = (text or "").strip()[:20]
        for ch in ["\\", "/", ":", "*", "?", '"', "<", ">", "|"]:
            title = title.replace(ch, "")
        return title or "新会话"
    except Exception:
        return "新会话"
