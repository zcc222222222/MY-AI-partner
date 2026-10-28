"""Manual knowledge maintenance. Every route is authenticated and admin-only."""
from typing import Literal
import sqlite3
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from ..auth import knowledge_admin
from ..services.auth_service import Principal
from ..services import knowledge_ops as ops
from ..services import retrieval_diagnostics as diagnostics, retrieval_recheck as recheck
from llmwiki.store import WikiError
from llmwiki.updates import UpdateBusy

router = APIRouter(prefix='/api/v1/knowledge/ops', tags=['knowledge-management'])


class JobIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    recipe: Literal['inspect','periods','official','roles','build']


class RecheckIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    topic: Literal['逆境深塔','冥歌海墟','终焉矩阵']


@router.get('/retrievals')
def retrievals(principal: Principal = Depends(knowledge_admin)):
    try: return {'records':diagnostics.recent()}
    except (OSError,ValueError): raise HTTPException(503,'查询记录暂不可读取') from None


@router.post('/retrievals/recheck',status_code=202)
def recheck_topic(body: RecheckIn, principal: Principal = Depends(knowledge_admin)):
    try: return recheck.start(body.topic)
    except recheck.CoolingDown as exc:
        raise HTTPException(429,f'查询冷却中，请在约{exc.seconds}秒后重试',headers={'Retry-After':str(exc.seconds)}) from None
    except UpdateBusy: raise HTTPException(409,'已有攻略查询运行中，请等待完成') from None
    except (OSError,ValueError,KeyError): raise HTTPException(503,'暂时无法启动攻略检查') from None


@router.get('')
def overview(principal: Principal = Depends(knowledge_admin)):
    try: return ops.snapshot()
    except (OSError, WikiError, ValueError, sqlite3.Error, KeyError, TypeError):
        raise HTTPException(503, '知识库状态暂不可读取，请检查本地数据是否已准备好') from None


@router.get('/jobs/{job_id}')
def job(job_id: str, principal: Principal = Depends(knowledge_admin)):
    try: return ops.public_job(ops.load_job(job_id))
    except (OSError, ValueError, KeyError):
        raise HTTPException(404, '任务不存在') from None


@router.post('/jobs', status_code=202)
def create(body: JobIn, principal: Principal = Depends(knowledge_admin)):
    try: return ops.start_job(body.recipe, principal.user_id)
    except UpdateBusy:
        raise HTTPException(409, '已有更新任务运行，请等待完成') from None
    except (OSError, WikiError, ValueError):
        raise HTTPException(503, '暂时无法创建更新任务') from None


@router.post('/jobs/{job_id}/resume', status_code=202)
def resume(job_id: str, principal: Principal = Depends(knowledge_admin)):
    try:
        job = ops.load_job(job_id)
        return ops.start_job(job['recipe'], principal.user_id, resume=job_id)
    except UpdateBusy:
        raise HTTPException(409, '已有更新任务运行，请等待完成') from None
    except (OSError, WikiError, ValueError, KeyError):
        raise HTTPException(400, '任务不存在或当前不能恢复') from None
