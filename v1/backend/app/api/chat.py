"""聊天与会话路由。"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import SessionLocal, get_db
from ..services import chat_service, session_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


def _sse(payload: dict) -> str:
    """把一个事件包成 SSE 帧。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


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
def send_message(session_id: str, body: schemas.MessageCreate):
    # 这里刻意不用请求级 Depends(get_db):请求级依赖的连接会活到整段流式响应结束,
    # 白白占着一条空闲连接。改成短生命周期探针,查完立刻释放。
    with SessionLocal() as probe:
        if session_service.get_session(probe, session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在")

    def event_stream():
        # 流式期间使用独立会话,避免与请求级依赖的线程/生命周期耦合。
        with SessionLocal() as stream_db:
            # 取值必须判空、且不能放在 try 之外:此时 200 响应头已经发出去了,
            # 这里再抛异常连接会被硬切断,前端只会看到「回答是空的、且没有任何报错」,
            # 这类现象极难排查。所以一律降级成一条 error 事件。
            session = session_service.get_session(stream_db, session_id)
            if session is None:
                yield _sse({"type": "error", "detail": "会话不存在,可能已被删除"})
                return
            character = stream_db.get(models.Character, session.character_id)
            if character is None:
                yield _sse({"type": "error", "detail": "角色卡不存在,请检查 characters/ 目录"})
                return
            try:
                for payload in chat_service.stream_reply(stream_db, session, character, body.content):
                    yield f"data: {payload}\n\n"
            except Exception as exc:  # noqa: BLE001
                logger.exception("chat.stream_failed session_id=%s", session_id)
                yield _sse({"type": "error", "detail": str(exc)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
