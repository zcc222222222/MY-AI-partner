"""聊天与会话路由。"""
import json
import logging
import uuid
from contextlib import closing

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import SessionLocal, get_db
from ..services import chat_service, session_service
from ..services import usage_service as usage
from sqlalchemy import select
from ..auth import current_user
from ..services.auth_service import Principal, validate_principal
from .generation_response import GenerationResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


@router.get('/quota')
def quota(db: Session = Depends(get_db), principal: Principal = Depends(current_user)):
    return usage.overview(db,principal.user_id)


@router.get('/requests/{client_id}')
def request_status(client_id: str, db: Session = Depends(get_db), principal: Principal = Depends(current_user)):
    row=db.scalar(select(models.GenerationRequest).where(models.GenerationRequest.owner_id==principal.user_id,
                    models.GenerationRequest.client_request_id==client_id))
    if row is None:raise HTTPException(404,'请求不存在')
    return usage.public_request(row)


def _sse(payload: dict) -> str:
    """把一个事件包成 SSE 帧。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.get("/sessions", response_model=list[schemas.SessionOut])
def list_sessions(db: Session = Depends(get_db), principal: Principal = Depends(current_user)):
    return session_service.list_sessions(db, owner_id=principal.user_id)


@router.post("/sessions", response_model=schemas.SessionOut)
def create_session(body: schemas.SessionCreate | None = None, db: Session = Depends(get_db), principal: Principal = Depends(current_user)):
    try:
        return session_service.create_session(db, body.character_id if body else None, owner_id=principal.user_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.delete("/sessions/{session_id}")
def remove_session(session_id: str, db: Session = Depends(get_db), principal: Principal = Depends(current_user)):
    if not session_service.delete_session(db, session_id, owner_id=principal.user_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"ok": True}


@router.get("/sessions/{session_id}/messages", response_model=list[schemas.MessageOut])
def list_messages(session_id: str, db: Session = Depends(get_db), principal: Principal = Depends(current_user)):
    if session_service.get_session(db, session_id, owner_id=principal.user_id) is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session_service.list_messages(db, session_id, owner_id=principal.user_id)


@router.post("/sessions/{session_id}/messages")
def send_message(session_id: str, body: schemas.MessageCreate, principal: Principal = Depends(current_user)):
    # 这里刻意不用请求级 Depends(get_db):请求级依赖的连接会活到整段流式响应结束,
    # 白白占着一条空闲连接。改成短生命周期探针,查完立刻释放。
    with SessionLocal() as probe:
        if session_service.get_session(probe, session_id, owner_id=principal.user_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在")

    info,fresh=usage.admit(SessionLocal,principal.user_id,session_id,body.client_request_id or uuid.uuid4().hex,body.content)
    if not fresh:
        if info['status']=='completed' and info['assistant_message_id']:
            with SessionLocal() as db:
                message=db.scalar(select(models.Message).where(models.Message.id==info['assistant_message_id'],models.Message.session_id==session_id))
                if message is not None:
                    replay=[_sse({'type':'replace','content':message.content}),_sse({'type':'done','message_id':message.id,'status':'complete'})]
                    return StreamingResponse(iter(replay),media_type='text/event-stream',headers={'Cache-Control':'no-store'})
        raise HTTPException(409,'该请求已受理，请查看会话记录或请求状态，勿重复提交')
    scope=usage.Scope(info['id'],SessionLocal)

    def assert_access():
        usage.check_cancelled()
        with SessionLocal() as check_db:
            validate_principal(check_db, principal)
            if session_service.get_session(check_db, session_id, owner_id=principal.user_id) is None:
                raise HTTPException(status_code=404, detail="会话不存在")

    def events():
        # 流式期间使用独立会话,避免与请求级依赖的线程/生命周期耦合。
        with SessionLocal() as stream_db:
            # 取值必须判空、且不能放在 try 之外:此时 200 响应头已经发出去了,
            # 这里再抛异常连接会被硬切断,前端只会看到「回答是空的、且没有任何报错」,
            # 这类现象极难排查。所以一律降级成一条 error 事件。
            session = session_service.get_session(stream_db, session_id, owner_id=principal.user_id)
            if session is None:
                yield _sse({"type": "error", "detail": "会话不存在,可能已被删除"})
                return
            character = stream_db.get(models.Character, session.character_id)
            if character is None:
                yield _sse({"type": "error", "detail": "角色卡不存在,请检查 characters/ 目录"})
                return
            try:
                with closing(chat_service.stream_reply(stream_db, session, character, body.content, authorize=assert_access)) as reply:
                    for payload in reply:
                        usage.check_cancelled()
                        yield f"data: {payload}\n\n"
            except HTTPException as exc:
                yield _sse({"type": "error", "detail": exc.detail, "status_code": exc.status_code})
            except Exception as exc:  # noqa: BLE001
                usage.check_cancelled()
                logger.exception("chat.stream_failed session_id=%s", session_id)
                yield _sse({"type": "error", "detail": "生成失败，请稍后重试"})

    def event_stream():
        source=events();status='cancelled';assistant_id=None
        try:
            while True:
                # Starlette may resume each next() in a different thread. Bind and reset
                # within each step, never carry a ContextVar token across an SSE yield.
                with usage.bound(scope):
                    if assistant_id is None:usage.heartbeat()
                    try:item=next(source)
                    except StopIteration:
                        status='completed' if assistant_id else 'failed'
                        break
                payload=json.loads(item.removeprefix('data: ').strip())
                if payload.get('type')=='done':
                    assistant_id=payload.get('message_id')
                    if assistant_id:usage.record_answer(scope,assistant_id)
                if payload.get('type')=='error':status='failed'
                yield item
        except HTTPException as exc:
            status='failed'
            yield _sse({'type':'error','detail':exc.detail,'status_code':exc.status_code})
        except usage.GenerationCancelled:
            status='cancelled'
        except GeneratorExit:
            raise
        except Exception:
            status='failed';logger.exception('chat.request_failed request_id=%s',scope.request_id)
            yield _sse({'type':'error','detail':'生成失败，请稍后重试'})
        finally:
            try:
                with usage.bound(scope):source.close()
            finally:
                usage.finish(scope,'completed' if assistant_id else 'cancelled' if scope.cancelled.is_set() else status,assistant_id=assistant_id)

    return GenerationResponse(
        event_stream(),
        scope,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
