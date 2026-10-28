"""SQLite/MySQL-backed admission, leases and conservative CNY accounting.

One micro = 0.000001 CNY. Rates are a versioned conservative peak estimate,
not a claim to reconcile the provider invoice. No prompts, keys or answers here.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import threading
import uuid

from fastapi import HTTPException
from sqlalchemy import select, update, func

from .. import models
from ..config import settings

ACTIVE = ('running',)
LOCAL = timezone(timedelta(hours=8))
CURRENT = ContextVar('usage_request',default=None)
RATES = {'deepseek-flash':(2_000_000,40_000,8_000_000),
         'deepseek-v4-pro':(9_000_000,300_000,27_000_000)}
PRICE_VERSION = 'deepseek-conservative-peak-2026-10-27'


class QuotaError(HTTPException):
    def __init__(self, detail, *, code=429, retry=60):
        super().__init__(code,detail,headers={'Retry-After':str(retry)} if code==429 else None)


class GenerationCancelled(BaseException):
    """Cooperative cancellation must bypass model/parser fallback handlers."""


@dataclass
class Scope:
    request_id: str
    factory: object
    last_heartbeat: datetime | None = None
    cancelled: threading.Event = field(default_factory=threading.Event, repr=False)
    _close_lock: object = field(default_factory=threading.Lock, repr=False)
    _closers: set = field(default_factory=set, repr=False)

    def cancel(self):
        with self._close_lock:
            self.cancelled.set()
            closers = tuple(self._closers)
        for close in closers:
            close()


def check_cancelled():
    scope = CURRENT.get()
    if scope and scope.cancelled.is_set():
        raise GenerationCancelled()


@contextmanager
def closing_response(response):
    """Close an individual upstream stream on disconnect, never the shared client."""
    scope = CURRENT.get()
    guard = threading.Lock()
    closed = False

    def close():
        nonlocal closed
        with guard:
            if closed:
                return
            closed = True
        try:
            callback = getattr(response, 'close', None)
            if callback:
                callback()
        except Exception:
            logging.getLogger(__name__).warning('upstream.close_failed', exc_info=True)

    if scope:
        with scope._close_lock:
            scope._closers.add(close)
            cancelled = scope.cancelled.is_set()
        if cancelled:
            close()
    try:
        check_cancelled()
        yield response
    finally:
        if scope:
            with scope._close_lock:
                scope._closers.discard(close)
        close()


@contextmanager
def bound(scope):
    token=CURRENT.set(scope)
    try: yield
    finally: CURRENT.reset(token)


def now(): return models.utcnow()


def keys(owner_id, moment):
    local=moment.replace(tzinfo=timezone.utc).astimezone(LOCAL)
    day=local.strftime('%Y-%m-%d');month=local.strftime('%Y-%m')
    return [f'user:{owner_id}:day:{day}',f'global:day:{day}',f'global:month:{month}']


def limits():
    return {'user_daily_questions':settings.quota_user_daily_questions,'global_daily_questions':settings.quota_global_daily_questions,
            'user_daily_micros':settings.quota_user_daily_micros,'global_daily_micros':settings.quota_global_daily_micros,
            'global_monthly_micros':settings.quota_global_monthly_micros,'user_rpm':settings.quota_user_rpm,
            'user_concurrency':settings.quota_user_concurrency,'global_concurrency':settings.quota_global_concurrency,
            'generation_timeout_seconds':settings.generation_timeout_seconds,'currency':'CNY','micros_per_yuan':1_000_000,
            'price_version':PRICE_VERSION,'pricing_basis':'conservative_peak_estimate'}


def lock(db):
    # This pre-created row serializes short ledger transactions, never model/network waits.
    if db.execute(update(models.QuotaBucket).where(models.QuotaBucket.key=='system-lock').values(updated_at=now())).rowcount!=1:
        raise QuotaError('用量账本尚未就绪，请联系管理员',code=503)


def bucket(db,key):
    row=db.get(models.QuotaBucket,key)
    if row is None:
        row=models.QuotaBucket(key=key,questions=0,spent_micros=0,reserved_micros=0)
        db.add(row);db.flush()
    return row


def _settle(db,call,usage):
    if call.status!='reserved':return
    valid=isinstance(usage,dict)
    prompt=usage.get('prompt_tokens') if valid else None
    output=usage.get('completion_tokens') if valid else None
    hit=usage.get('prompt_cache_hit_tokens') if valid else None
    if hit is None and valid:
        details=usage.get('prompt_tokens_details') or {}
        hit=details.get('cached_tokens') if isinstance(details,dict) else None
    hit=0 if hit is None else hit
    valid=all(type(n) is int and n>=0 for n in (prompt,output,hit)) and hit<=prompt if valid else False
    if valid:
        cost=((prompt-hit)*call.input_rate+hit*call.cached_rate+output*call.output_rate+999999)//1000000
        call.prompt_tokens=prompt;call.completion_tokens=output;call.cache_hit_tokens=hit
        call.status='reported' if cost<=call.reserved_micros else 'reported_over_reserve'
    else:
        cost=call.reserved_micros;call.status='estimated_unknown'
    call.charged_micros=cost
    for key in json.loads(call.buckets):
        row=bucket(db,key);row.reserved_micros-=call.reserved_micros;row.spent_micros+=cost


def _finish(db,request,status,error=None):
    if request.status not in ACTIVE:return
    calls=db.scalars(select(models.LLMUsage).where(models.LLMUsage.request_id==request.id)).all()
    for call in calls:_settle(db,call,None)
    if not calls and status!='completed':
        for key in json.loads(request.question_buckets):bucket(db,key).questions-=1
    request.status=status;request.finished_at=now();request.error_code=error


def expire(db):
    for request in db.scalars(select(models.GenerationRequest).where(
            models.GenerationRequest.status.in_(ACTIVE),models.GenerationRequest.lease_until<=now())).all():
        _finish(db,request,'completed' if request.assistant_message_id else 'interrupted','lease_expired')
    db.flush()


def admit(factory,owner_id,session_id,client_id,content):
    digest=hashlib.sha256((session_id+'\0'+content).encode()).hexdigest()
    with factory() as db,db.begin():
        lock(db);expire(db)
        previous=db.scalar(select(models.GenerationRequest).where(models.GenerationRequest.owner_id==owner_id,
                    models.GenerationRequest.client_request_id==client_id))
        if previous:
            if previous.input_digest!=digest:raise QuotaError('同一请求ID不能用于不同消息',code=409)
            return public_request(previous),False
        if bucket(db,'system-lock').questions or bucket(db,f'pause:user:{owner_id}').questions:
            raise QuotaError('当前已暂停模型调用，请联系管理员',code=503)
        running=db.scalars(select(models.GenerationRequest).where(models.GenerationRequest.status.in_(ACTIVE))).all()
        if any(r.session_id==session_id for r in running):raise QuotaError('此会话已有回答正在生成',code=409)
        if sum(r.owner_id==owner_id for r in running)>=settings.quota_user_concurrency:
            raise QuotaError('当前账号已有回答正在生成，请等待完成')
        if len(running)>=settings.quota_global_concurrency:raise QuotaError('当前体验人数较多，请稍后再试')
        moment=now()
        recent=db.scalar(select(func.count()).select_from(models.GenerationRequest).where(
            models.GenerationRequest.owner_id==owner_id,models.GenerationRequest.created_at>moment-timedelta(seconds=60)))
        if recent>=settings.quota_user_rpm:raise QuotaError(f'每分钟最多发送{settings.quota_user_rpm}条消息，请稍后再试')
        names=keys(owner_id,moment);rows=[bucket(db,key) for key in names]
        for row,cap in zip(rows[:2],(settings.quota_user_daily_questions,settings.quota_global_daily_questions)):
            if row.questions>=cap:raise QuotaError('今日提问额度已用完，北京时间零点重置',retry=_reset_seconds(moment))
        for row,cap in zip(rows,(settings.quota_user_daily_micros,settings.quota_global_daily_micros,settings.quota_global_monthly_micros)):
            if row.spent_micros+row.reserved_micros>=cap:raise QuotaError('模型费用预算已用完，请稍后再试')
        for row in rows[:2]:row.questions+=1
        request=models.GenerationRequest(id=uuid.uuid4().hex,owner_id=owner_id,session_id=session_id,
            client_request_id=client_id,input_digest=digest,status='running',question_buckets=json.dumps(names[:2]),
            created_at=moment,lease_until=moment+timedelta(seconds=300))
        db.add(request);db.flush()
        return public_request(request),True


def _reset_seconds(moment):
    local=moment.replace(tzinfo=timezone.utc).astimezone(LOCAL)
    return max(1,int(((local+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)-local).total_seconds()))


def heartbeat(force=False):
    check_cancelled()
    scope=CURRENT.get()
    if not scope:return
    moment=now()
    if not force and scope.last_heartbeat and (moment-scope.last_heartbeat).total_seconds()<15:return
    with scope.factory() as db,db.begin():
        lock(db);expire(db)
        row=db.get(models.GenerationRequest,scope.request_id)
        if not row or row.status not in ACTIVE:raise QuotaError('生成任务已结束，请重新发送',code=409)
        if moment-row.created_at>timedelta(seconds=settings.generation_timeout_seconds):
            raise QuotaError('本轮生成超过时限，请缩小问题后重试',code=504)
        row.lease_until=moment+timedelta(seconds=300)
    scope.last_heartbeat=moment


def reserve_call(kwargs,purpose):
    scope=CURRENT.get()
    if not scope:return None
    heartbeat(force=True)
    model=kwargs.get('model',settings.deepseek_model)
    if model not in RATES:raise QuotaError('此模型尚未配置费用估算，已停止调用',code=503)
    rates=RATES[model]
    # UTF-8 byte count plus protocol margin is deliberately above our normal token estimator.
    input_bound=len(json.dumps(kwargs,ensure_ascii=False,default=str).encode())+4096
    output_bound=kwargs.get('max_tokens')
    if type(output_bound) is not int or output_bound<=0:raise QuotaError('模型输出预算缺失，已停止调用',code=503)
    amount=(input_bound*rates[0]+output_bound*rates[2]+999999)//1000000
    with scope.factory() as db,db.begin():
        lock(db);expire(db)
        request=db.get(models.GenerationRequest,scope.request_id)
        if not request or request.status not in ACTIVE:raise QuotaError('生成任务已结束',code=409)
        if bucket(db,'system-lock').questions or bucket(db,f'pause:user:{request.owner_id}').questions:
            raise QuotaError('当前已暂停模型调用',code=503)
        names=keys(request.owner_id,now());rows=[bucket(db,key) for key in names]
        for row,cap in zip(rows,(settings.quota_user_daily_micros,settings.quota_global_daily_micros,settings.quota_global_monthly_micros)):
            if row.spent_micros+row.reserved_micros+amount>cap:
                raise QuotaError('剩余模型预算不足以完成本次调用，请稍后再试')
        for row in rows:row.reserved_micros+=amount
        call=models.LLMUsage(id=uuid.uuid4().hex,request_id=request.id,purpose=purpose,model=model,status='reserved',
            buckets=json.dumps(names),reserved_micros=amount,charged_micros=0,price_version=PRICE_VERSION,
            input_rate=rates[0],cached_rate=rates[1],output_rate=rates[2])
        db.add(call);db.flush()
        return call.id


def settle_call(call_id,usage):
    scope=CURRENT.get()
    if not scope or not call_id:return
    if usage is not None and hasattr(usage,'model_dump'):usage=usage.model_dump()
    with scope.factory() as db,db.begin():
        lock(db)
        call=db.get(models.LLMUsage,call_id)
        if not call or call.request_id!=scope.request_id:raise RuntimeError('Usage ownership mismatch')
        _settle(db,call,usage)


def finish(scope,status,error=None,assistant_id=None):
    with scope.factory() as db,db.begin():
        lock(db)
        request=db.get(models.GenerationRequest,scope.request_id)
        if not request:return
        if assistant_id:request.assistant_message_id=assistant_id
        if request.assistant_message_id:status='completed'
        _finish(db,request,status,error)


def record_answer(scope,assistant_id):
    with scope.factory() as db,db.begin():
        lock(db)
        request=db.get(models.GenerationRequest,scope.request_id)
        if request and request.status in ACTIVE:request.assistant_message_id=assistant_id


def record_user(message_id):
    scope=CURRENT.get()
    if not scope:return
    with scope.factory() as db,db.begin():
        lock(db)
        request=db.get(models.GenerationRequest,scope.request_id)
        if request and request.status in ACTIVE:request.user_message_id=message_id


def context_history(db,history,owner_id,session_id):
    if not CURRENT.get():return history
    omitted=set(db.scalars(select(models.GenerationRequest.user_message_id).where(
        models.GenerationRequest.owner_id==owner_id,models.GenerationRequest.session_id==session_id,
        models.GenerationRequest.status.in_(('failed','cancelled','interrupted')))))
    return [m for m in history if m.id not in omitted]


def public_request(row):
    return {key:getattr(row,key) for key in ('id','client_request_id','status','user_message_id','assistant_message_id','error_code')}


def overview(db,owner_id,*,admin=False):
    moment=now();names=keys(owner_id,moment);data=[]
    caps=(settings.quota_user_daily_micros,settings.quota_global_daily_micros,settings.quota_global_monthly_micros)
    for name,cap in zip(names,caps):
        row=db.get(models.QuotaBucket,name)
        spent=row.spent_micros if row else 0;reserved=row.reserved_micros if row else 0
        data.append({'scope':name.split(':')[0],'period':name.rsplit(':',1)[1],
            'questions':row.questions if row else 0,'spent_micros':spent,'reserved_micros':reserved,
            'budget_micros':cap,'remaining_micros':max(0,cap-spent-reserved)})
    control=db.get(models.QuotaBucket,'system-lock')
    paused=db.get(models.QuotaBucket,f'pause:user:{owner_id}')
    result={'limits':limits(),'user':data[0],'paused':bool(control and control.questions or paused and paused.questions),
            'reset_timezone':'Asia/Shanghai','remaining_questions':max(0,settings.quota_user_daily_questions-data[0]['questions'])}
    if admin:
        result.update(global_day=data[1],global_month=data[2],global_paused=bool(control and control.questions))
        user_rows=db.scalars(select(models.QuotaBucket).where(models.QuotaBucket.key.like('user:%:day:'+names[0].rsplit(':',1)[1]))
                        .order_by(models.QuotaBucket.spent_micros.desc()).limit(100)).all()
        result['users']=[]
        for row in user_rows:
            uid=int(row.key.split(':')[1]);user=db.get(models.User,uid)
            blocked=db.get(models.QuotaBucket,f'pause:user:{uid}')
            result['users'].append({'owner_id':uid,'username':user.username if user else str(uid),'questions':row.questions,
                'spent_micros':row.spent_micros,'reserved_micros':row.reserved_micros,'paused':bool(blocked and blocked.questions)})
        requests=db.scalars(select(models.GenerationRequest).order_by(models.GenerationRequest.created_at.desc()).limit(50)).all()
        result['requests']=[{**public_request(r),'owner_id':r.owner_id,'created_at':r.created_at,
            'charged_micros':db.scalar(select(func.coalesce(func.sum(models.LLMUsage.charged_micros),0)).where(models.LLMUsage.request_id==r.id)),
            'unknown_calls':db.scalar(select(func.count()).select_from(models.LLMUsage).where(models.LLMUsage.request_id==r.id,models.LLMUsage.status=='estimated_unknown'))} for r in requests]
    return result


def pause(db,paused,owner_id=None):
    lock(db)
    if owner_id is not None and db.get(models.User,owner_id) is None:raise QuotaError('账号不存在',code=404)
    bucket(db,'system-lock' if owner_id is None else f'pause:user:{owner_id}').questions=int(paused)
    db.commit()
