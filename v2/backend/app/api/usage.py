"""Read-only personal allowance and authenticated operator controls."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..auth import knowledge_admin
from ..database import get_db
from ..services.auth_service import Principal
from ..services import usage_service as usage
from .. import models

router=APIRouter(prefix='/api/v1/usage',tags=['usage-management'])


class PauseIn(BaseModel):
    model_config=ConfigDict(extra='forbid')
    paused: bool
    owner_id: int | None=Field(default=None,gt=0)


@router.get('/admin')
def overview(db: Session=Depends(get_db),principal: Principal=Depends(knowledge_admin)):
    return usage.overview(db,principal.user_id,admin=True)


@router.post('/admin/pause')
def pause(body: PauseIn,db: Session=Depends(get_db),principal: Principal=Depends(knowledge_admin)):
    usage.pause(db,body.paused,body.owner_id)
    return {'paused':body.paused,'owner_id':body.owner_id}


@router.get('/admin/requests/{request_id}')
def calls(request_id: str,db: Session=Depends(get_db),principal: Principal=Depends(knowledge_admin)):
    if not db.get(models.GenerationRequest,request_id):raise HTTPException(404,'请求不存在')
    rows=db.scalars(select(models.LLMUsage).where(models.LLMUsage.request_id==request_id).order_by(models.LLMUsage.created_at)).all()
    return {'calls':[{k:getattr(row,k) for k in ('id','purpose','model','status','reserved_micros','charged_micros',
            'prompt_tokens','completion_tokens','cache_hit_tokens','price_version')} for row in rows]}
