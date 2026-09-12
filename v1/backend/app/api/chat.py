"""聊天与会话路由。"""
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import SessionLocal, get_db
from ..services import chat_service, session_service

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


@router.get("/sessions", response_model=list[schemas.SessionOut])
def list_sessions(db: Session = Depends(get_db)):
    return session_service.list_sessions(db)


@router.post("/sessions", response_model=schemas.SessionOut)
def create_session(body: schemas.SessionCreate | None = None, db: Session = Depends(get_db)):
    try:
        return session_service.create_session(db, body.character_id if body else None)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.delete("/sessions/{session_id}")
def remove_session(session_id: str, db: Session = Depends(get_db)):
    if not session_service.delete_session(db, session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"ok": True}


@router.get("/sessions/{session_id}/messages", response_model=list[schemas.MessageOut])
def list_messages(session_id: str, db: Session = Depends(get_db)):
    if session_service.get_session(db, session_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session_service.list_messages(db, session_id)


@router.post("/sessions/{session_id}/messages")
def send_message(session_id: str, body: schemas.MessageCreate, db: Session = Depends(get_db)):
    if session_service.get_session(db, session_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")

    def event_stream():
        # 流式期间使用独立会话,避免与请求级依赖的线程/生命周期耦合。
        with SessionLocal() as stream_db:
            session = session_service.get_session(stream_db, session_id)
            character = stream_db.get(models.Character, session.character_id)
            try:
                for payload in chat_service.stream_reply(stream_db, session, character, body.content):
                    yield f"data: {payload}\n\n"
            except Exception as exc:  # noqa: BLE001
                yield f"data: {json.dumps({'type': 'error', 'detail': str(exc)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
