"""Bounded operational metadata; never store queries, tool arguments or answers."""
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import time
import uuid

from knowledge.config import DATA_DIR
from llmwiki.updates import atomic_json

ROOT = Path(os.getenv('RETRIEVAL_DIAGNOSTICS_DIR') or str(Path(DATA_DIR)/'retrieval-diagnostics'))
OBSERVATION = ContextVar('retrieval_observation',default=None)
FORCE_REFRESH = ContextVar('retrieval_force_refresh',default=False)
TOPICS = ('逆境深塔','冥歌海墟','终焉矩阵')
REASONS = {'success','no_match','period_unverified','scope_unverified','cooldown','timeout',
           'access_restricted','network_error','source_changed','robots_disallowed','dependency_missing',
           'source_unavailable','invalid_request','insufficient_evidence','internal_error'}
STAGES = {'cache','local','robots','catalog','navigation','search_response','sort','worker','launch','tool','validation','cooldown','initializing'}
CACHES = {'none','local','memory','disk','network'}
logger = logging.getLogger(__name__)


def now(): return datetime.now(timezone.utc).isoformat()


def observe(**fields):
    state = OBSERVATION.get()
    if state is None: return
    for key,allowed in (('reason',REASONS),('stage',STAGES),('cache',CACHES)):
        if fields.get(key) in allowed: state[key]=fields[key]


def topic_for(arguments):
    query = arguments.get('query','') if isinstance(arguments,dict) else ''
    if not isinstance(query,str): return None
    matches=[name for name,aliases in (('逆境深塔',('逆境深塔','深塔')),('冥歌海墟',('冥歌海墟','海墟')),('终焉矩阵',('终焉矩阵','矩阵')))
             if any(alias in query for alias in aliases)]
    return matches[0] if len(matches)==1 else None


def path_for(run_id):
    if not isinstance(run_id,str) or not re.fullmatch('[a-f0-9]{32}',run_id): raise ValueError('Invalid diagnostic ID')
    path = ROOT/(run_id+'.json')
    if path.resolve().parent != ROOT.resolve(): raise ValueError('Invalid diagnostic path')
    return path


def load(run_id):
    path=path_for(run_id)
    if path.stat().st_size>4096: raise ValueError('Invalid diagnostic size')
    data=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data,dict) or data.get('id')!=run_id or data.get('schema')!='retrieval-diagnostic-v1': raise ValueError('Invalid diagnostic record')
    return data


def public(data):
    keys=('id','tool','topic','origin','started_at','finished_at','elapsed_ms','result_count','image_count','cache','stage','reason','retry_after_seconds')
    result={k:data.get(k) for k in keys}
    result['state']=data['state']
    if result['state']=='running':
        # Lazy import avoids a module cycle; this probe is read-only on Windows too.
        from .knowledge_ops import alive
        if not alive(data.get('pid')): result['state']='interrupted'
    result['can_retry']=bool(result.get('topic') in TOPICS and result['state']!='running')
    return result


def recent(limit=20):
    if not ROOT.is_dir(): return []
    result=[]
    paths=[p for p in ROOT.glob('*.json') if re.fullmatch('[a-f0-9]{32}',p.stem)]
    for path in sorted(paths,key=lambda p:p.stat().st_mtime,reverse=True)[:300]:
        try: result.append(public(load(path.stem)))
        except (OSError,ValueError,KeyError,TypeError): continue
        if len(result)>=limit: break
    return result


def begin(tool,arguments,origin='chat'):
    run_id=uuid.uuid4().hex
    record={'schema':'retrieval-diagnostic-v1','id':run_id,'tool':tool,'topic':topic_for(arguments),'origin':origin,
            'state':'running','started_at':now(),'pid':os.getpid(),'cache':'none' if tool=='find_gameplay_guides' else 'local'}
    try:
        ROOT.mkdir(parents=True,exist_ok=True)
        atomic_json(path_for(run_id),record)
        paths=[p for p in ROOT.glob('*.json') if re.fullmatch('[a-f0-9]{32}',p.stem)]
        protected=None
        try: protected=json.loads((ROOT/'control/last-recheck.json').read_text(encoding='utf-8')).get('id')
        except (OSError,ValueError,AttributeError): pass
        ordered=sorted(paths,key=lambda p:p.stat().st_mtime,reverse=True)
        keep={p.stem for p in ordered[:300]}
        if len(ordered)>300 and protected and protected not in keep and any(p.stem==protected for p in ordered):
            keep.discard(ordered[299].stem)
            keep.add(protected)
        for old in ordered:
            if old.stem in keep: continue
            try: load(old.stem)
            except (OSError,ValueError,KeyError,TypeError): continue
            old.unlink(missing_ok=True)
    except OSError: logger.warning('retrieval_diagnostics_write_unavailable')
    return record


def classify(payload,state):
    if not payload: return 'internal_error'
    freshness = payload.get('freshness')
    if isinstance(freshness,dict) and freshness.get('mode')=='last_success_after_refresh_failure':
        reason = freshness.get('refresh_failure_reason')
        if reason in REASONS: return reason
    code=payload.get('status') or payload.get('error')
    if not isinstance(code,(str,type(None))): return 'internal_error'
    if code in ('period_unverified','scope_unverified','cooldown','access_restricted'): return code
    if code=='source_unavailable': return state.get('reason','source_unavailable')
    if code in ('no_reviewed_match','not_found_in_catalog','not_found_in_community','not_found','no_match'): return 'no_match'
    if code in ('needs_period','needs_topic','ambiguous_period','ambiguous_topic','invalid_query','invalid_scope','invalid_entity'):
        return 'invalid_request'
    if code in ('uncovered_phase_request','uncovered_rotation_request','entity_unresolved'): return 'insufficient_evidence'
    if payload.get('error') and ('suggestions' in payload or 'candidates' in payload): return 'invalid_request'
    if payload.get('error'): return 'internal_error'
    if isinstance(payload.get('results'),list) and not payload['results']: return 'no_match'
    return 'success'


def run(tool,arguments,call,*,record=None):
    record=record or begin(tool,arguments)
    state={'cache':record['cache'],'stage':'tool' if tool=='find_gameplay_guides' else 'local'}
    token=OBSERVATION.set(state);started=time.monotonic()
    try:
        result=call()
        payload=result if isinstance(result,dict) else {}
        reason=classify(payload,state)
        rows=payload.get('results',[])
        count=len(rows) if isinstance(rows,list) else 0
        if 'results' not in payload and reason=='success' and payload: count=1
        images=sum(len(row['images']) for row in rows if isinstance(row,dict) and isinstance(row.get('images'),list)) if isinstance(rows,list) else 0
        retry=payload.get('retry_after_seconds',0)
        record.update(state='completed',reason=reason,result_count=count,image_count=images,
                      retry_after_seconds=retry if type(retry) is int and 0<=retry<=604800 else 0)
        return result
    except BaseException as exc:
        record.update(state='failed' if isinstance(exc,Exception) else 'interrupted',
                      reason=state.get('reason','internal_error'),result_count=0,image_count=0)
        raise
    finally:
        record.update(cache=state['cache'],stage=state['stage'],finished_at=now(),elapsed_ms=round((time.monotonic()-started)*1000))
        OBSERVATION.reset(token)
        try: atomic_json(path_for(record['id']),record)
        except OSError: logger.warning('retrieval_diagnostics_write_unavailable')
