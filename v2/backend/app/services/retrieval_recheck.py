"""Admin-triggered, bounded live checks; never replay private query arguments."""
from datetime import datetime, timezone
import json
import threading
from . import retrieval_diagnostics as diagnostics
from llmwiki.updates import UpdateLock, UpdateBusy, atomic_json


class CoolingDown(ValueError):
    def __init__(self,seconds): self.seconds=max(1,int(seconds))


def execute(record):
    from ..agents.live_guides import find_gameplay_guides
    token=diagnostics.FORCE_REFRESH.set(True)
    try:
        diagnostics.run('find_gameplay_guides',{'query':record['topic']},
                        lambda:find_gameplay_guides('本期'+record['topic']+'攻略'),record=record)
    except Exception:
        pass  # Safe failure already persisted by the wrapper.
    finally: diagnostics.FORCE_REFRESH.reset(token)


def start(topic,*,dispatch=None):
    from ..agents.live_guides import CLIENT
    if topic not in diagnostics.TOPICS: raise ValueError('请选择已支持的玩法')
    control=diagnostics.ROOT/'control'
    with UpdateLock(control/'dispatch'):
        state=CLIENT.health()
        if state['status']=='running': raise UpdateBusy('查询正在运行')
        if state['wait_seconds']>0: raise CoolingDown(state['wait_seconds'])
        previous=control/'last-recheck.json'
        if previous.is_file():
            saved=json.loads(previous.read_text(encoding='utf-8'))
            prior=diagnostics.public(diagnostics.load(saved['id']))
            if prior['state']=='running': raise UpdateBusy('查询正在运行')
            if prior.get('finished_at') and prior.get('retry_after_seconds'):
                age=(datetime.now(timezone.utc)-datetime.fromisoformat(prior['finished_at'])).total_seconds()
                remaining=prior['retry_after_seconds']-age
                if remaining>0: raise CoolingDown(remaining+1)
            elapsed=(datetime.now(timezone.utc)-datetime.fromisoformat(saved['started_at'])).total_seconds()
            if elapsed<60: raise CoolingDown(60-elapsed+1)
        record=diagnostics.begin('find_gameplay_guides',{'query':topic},origin='admin_recheck')
        atomic_json(previous,{'id':record['id'],'started_at':record['started_at']})
        try:
            if dispatch: dispatch(record)
            else: threading.Thread(target=execute,args=(record,),daemon=True).start()
        except Exception:
            record.update(state='failed',reason='internal_error',finished_at=diagnostics.now())
            atomic_json(diagnostics.path_for(record['id']),record)
            raise OSError('Recheck dispatch unavailable') from None
        return diagnostics.public(record)
