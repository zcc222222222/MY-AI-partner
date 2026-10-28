"""Administrator operations: read-only inventory and durable, bounded manual jobs."""
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import threading
import uuid

from ..config import settings

from knowledge.config import DATA_DIR
from llmwiki.store import WikiStore, WikiError
from llmwiki.updates import UpdateLock, UpdateBusy, UpdateOptions, atomic_json, plan_documents, run_update
from ..agents.guide_periods import current_period

WIKI_PATH = Path(DATA_DIR) / 'wiki/wiki.db'
KNOWLEDGE_PATH = Path(DATA_DIR) / 'knowledge.db'
ROOT = Path(DATA_DIR) / 'wiki/operations'
CACHE = Path(DATA_DIR) / 'source-collection'
BACKEND = Path(__file__).resolve().parents[2]
ACTIVE = {'queued', 'running'}
RECIPES = {
    'inspect': {'title': '检查本地更新计划', 'description': '不联网、不调用模型，检查待更新章节与复核状态。', 'live': False},
    'periods': {'title': '刷新玩法周期与活动日历', 'description': '检查深塔、海墟、矩阵周期及活动日历，最多12章。', 'live': True},
    'official': {'title': '检查最新官方公告', 'description': '最多采集3篇官网公告，构建待更新章节，最多12章。', 'live': True},
    'roles': {'title': '同步全角色最新攻略', 'description': '刷新官方攻略目录，逐角色选择最新一篇；可能运行较久。', 'live': True},
    'build': {'title': '处理已导入的待更新资料', 'description': '不重新采集，编译审核本地待更新章节，最多12章。', 'live': True},
}
SUMMARY_KEYS = {'new_sources','changed_sources','unchanged_sources','published_pages','unchanged_pages',
    'needs_attention','retryable_pages','deferred_pages','http_requests','cache_hits','collection_failures',
    'completed','pending','retryable_failure','unavailable','sources','pages','dirty_pages','pending_pages'}


def now(): return datetime.now(timezone.utc).isoformat()


def stamp(value):
    try:
        result = datetime.fromisoformat(value)
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError): return None


class ReadOnlyWiki(WikiStore):
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.require_existing = True
        if not self.path.is_file(): raise WikiError('Wiki数据库尚未准备好')

    @contextmanager
    def connection(self, write=False):
        if write: raise WikiError('管理状态查询禁止写入Wiki')
        with closing(sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True, timeout=5)) as c:
            c.row_factory = sqlite3.Row
            yield c

    def refresh(self, **kwargs):
        raise WikiError('管理状态查询禁止触发刷新')


def read_json(path):
    if path.stat().st_size > 12_000_000: raise ValueError('Report too large')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict): raise ValueError('Invalid report')
    return value


def valid_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{32}', value):
        raise ValueError('无效任务编号')
    return value


def job_path(job_id):
    path = ROOT / (valid_id(job_id) + '.json')
    if path.resolve().parent != ROOT.resolve(): raise ValueError('无效任务路径')
    return path


def load_job(job_id): return read_json(job_path(job_id))


def alive(pid):
    if type(pid) is not int or pid <= 0: return False
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle: return False
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally: kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError): return False


def effective_state(job):
    state = job['state']
    if state not in ACTIVE: return state
    touched = stamp(job.get('heartbeat_at') or job.get('updated_at') or job.get('created_at'))
    age = (datetime.now(timezone.utc)-touched).total_seconds() if touched else float('inf')
    if state == 'queued': return 'interrupted' if age > 60 else state
    return state if age <= 120 and alive(job.get('pid')) else 'interrupted'


def report_file(job):
    run_id = job.get('report_id')
    if not run_id: return None
    folder = 'latest-guides' if job['recipe'] == 'roles' else 'updates'
    base = WIKI_PATH.parent / folder
    path = base / valid_id(run_id) / 'report.json'
    if not path.resolve().is_relative_to(base.resolve()): raise ValueError('无效报告路径')
    return path


def safe_report(report):
    result = {'state': report.get('state'), 'phase': report.get('phase'),
              'summary': {k:v for k,v in report.get('summary', {}).items() if k in SUMMARY_KEYS and type(v) is int},
              'collection': [], 'issues': []}
    for site, entry in report.get('collection', {}).items():
        if site not in ('mcguide', 'bwiki', 'official'): continue
        result['collection'].append({'site': site, 'status': entry.get('status'),
            'imported': len(entry.get('imported', [])), 'finished_at': entry.get('finished_at')})
        for error in entry.get('errors', [])[:5]:
            retry_at = error.get('retry_at')
            if type(retry_at) in (int, float):
                try: retry_at = datetime.fromtimestamp(retry_at, timezone.utc).isoformat()
                except (ValueError, OverflowError, OSError): retry_at = None
            result['issues'].append({'label': site, 'status': entry.get('status'),
                'retryable': bool(error.get('retryable')), 'retry_at': retry_at})
    for page in report.get('pages', []):
        if page.get('status') not in ('published', 'unchanged'):
            result['issues'].append({'label': str(page.get('title') or page.get('page_id', '章节'))[:180],
                'status': page.get('status'), 'retryable': bool(page.get('error', {}).get('retryable'))})
    for role in report.get('roles', []):
        if role.get('status') != 'completed':
            result['issues'].append({'label': str(role.get('name', '角色'))[:80], 'status': role.get('status')})
    for issue in report.get('errors', [])[:20]:
        result['issues'].append({'label':str(issue.get('name') or issue.get('source_id') or '目录映射')[:160], 'status':'needs_attention'})
    for issue in report.get('planning_errors', [])[:20]:
        result['issues'].append({'label':str(issue.get('source_id') or '本地规划')[:160], 'status':'needs_attention'})
    result['issues'] = result['issues'][:50]
    result['impact_reviews'] = report.get('impact_reviews', {}).get('counts', {})
    return result


def public_job(job):
    result = {k: job.get(k) for k in ('id','recipe','created_by','created_at','updated_at','finished_at','attempt', 'phase')}
    result['state'] = effective_state(job)
    result['error'] = job.get('error')  # Controlled messages only, never exception text.
    result['progress'] = {k: job.get('progress', {}).get(k) for k in ('site','page_status','name')}
    path = report_file(job)
    if path and path.is_file():
        result['report'] = safe_report(read_json(path))
    elif job.get('result'):
        result['report'] = job['result']
    result['can_resume'] = result['state'] in ('partial','failed','interrupted')
    return result


def list_jobs(limit=20):
    if not ROOT.is_dir(): return []
    result = []
    for path in sorted(ROOT.glob('*.json'), key=lambda p:p.stat().st_mtime, reverse=True)[:50]:
        try:
            job = load_job(path.stem)
            if job.get('id') != path.stem or job.get('recipe') not in RECIPES: continue
            result.append(public_job(job))
        except (OSError, ValueError, KeyError): continue
        if len(result) >= limit: break
    return result


def start_job(recipe, user_id, *, resume=None, launch=None):
    if recipe not in RECIPES: raise ValueError('请选择已提供的更新任务')
    ROOT.mkdir(parents=True, exist_ok=True)
    with UpdateLock(ROOT / 'dispatch'):
        if any(j['state'] in ACTIVE for j in list_jobs(50)):
            raise UpdateBusy('已有知识更新任务运行，请等待完成')
        if resume:
            job = load_job(resume)
            if job['recipe'] != recipe or effective_state(job) not in ('partial','failed','interrupted'):
                raise ValueError('该任务不能恢复')
            job['attempt'] += 1
        else:
            job = {'id':uuid.uuid4().hex,'recipe':recipe,'created_by':user_id,'created_at':now(),'attempt':1}
        job['requested_by'] = user_id
        job.update(state='queued',updated_at=now(),phase='queued',pid=None,heartbeat_at=None,error=None,finished_at=None)
        atomic_json(job_path(job['id']),job)
        try:
            log = ROOT / (job['id']+'.log')
            with log.open('ab') as output:
                process = (launch or subprocess.Popen)([sys.executable,'-m','app.services.knowledge_ops',job['id']],
                    cwd=str(BACKEND),stdin=subprocess.DEVNULL,stdout=output,stderr=output,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                if launch is None:
                    threading.Thread(target=process.wait,daemon=True).start()
        except OSError:
            job.update(state='failed',error='任务进程未能启动',updated_at=now(),finished_at=now())
            atomic_json(job_path(job['id']),job)
        return public_job(load_job(job['id']))


def snapshot():
    from ..agents.live_guides import CLIENT
    store = ReadOnlyWiki(WIKI_PATH)
    plan = plan_documents(store)
    with store.connection() as c:
        counts = {'sources':c.execute('''SELECT COUNT(*) FROM sources s JOIN source_revisions r
                    ON r.source_id=s.source_id AND r.game_version=s.game_version AND r.locale=s.locale
                    AND r.revision=s.current_revision WHERE r.deleted=0''').fetchone()[0],
                  'pages':c.execute('SELECT COUNT(*) FROM pages').fetchone()[0],
                  'dirty_pages':c.execute('SELECT COUNT(*) FROM pages WHERE dirty!=0').fetchone()[0]}
        revision_dates = {r[0]:r[1] for r in c.execute('''SELECT CASE WHEN s.source_id LIKE 'mcguide/%' THEN 'mcguide'
            WHEN s.source_id LIKE 'bwiki/%' THEN 'bwiki' ELSE 'official' END,MAX(r.created_at)
            FROM sources s JOIN source_revisions r ON r.source_id=s.source_id AND r.game_version=s.game_version
              AND r.locale=s.locale AND r.revision=s.current_revision
            WHERE r.deleted=0 AND (s.source_id LIKE 'mcguide/%' OR s.source_id LIKE 'bwiki/%' OR s.source_id LIKE 'official/%') GROUP BY 1''')}
    # A new immutable revision is not a successful recent crawl. Inspect collection checkpoints separately.
    checked = {}
    reports = WIKI_PATH.parent / 'updates'
    paths = sorted(reports.glob('*/report.json'), key=lambda p:p.stat().st_mtime, reverse=True)[:200] if reports.is_dir() else []
    for path in paths:
        try:
            for site, entry in read_json(path).get('collection', {}).items():
                if site not in ('mcguide','bwiki','official') or not entry.get('finished_at'): continue
                if site not in checked or entry['finished_at'] > checked[site]['checked_at']:
                    checked[site] = {'checked_at':entry['finished_at'],'last_status':entry['status']}
        except (OSError,ValueError,KeyError): pass
    sources = []
    for site, days in (('mcguide',7),('bwiki',1),('official',1)):
        item = {'site':site,'last_revision_at':revision_dates.get(site),**checked.get(site,{})}
        last = stamp(item.get('checked_at'))
        age = (datetime.now(timezone.utc)-last).total_seconds()/86400 if last else None
        item['freshness'] = 'not_checked' if age is None else 'failed' if item.get('last_status')!='completed' else 'stale' if age>days else 'recent'
        item['check_after_days'] = days
        sources.append(item)
    periods = []
    for topic in ('逆境深塔','冥歌海墟','终焉矩阵'):
        period = current_period(topic,path=WIKI_PATH)
        periods.append({'topic':topic,'status':'current' if period else 'unavailable',
                        **({k:period[k] for k in ('start','end','source_name')} if period else {})})
    counts['pending_pages'] = sum(p['status']!='unchanged' for p in plan['pages'])
    from .retrieval_diagnostics import recent
    return {'checked_at':now(),'counts':counts,'sources':sources,'periods':periods,'live_guides':CLIENT.health(),'retrievals':recent(),
            'pending_pages':[{k:p.get(k) for k in ('page_id','title','status')} for p in plan['pages'] if p['status']!='unchanged'][:50],
            'planning_error_count':len(plan['errors']),'impact_reviews':plan.get('impact_reviews',{}).get('counts',{}),
            'jobs':list_jobs(),'recipes':[{'id':k,**v} for k,v in RECIPES.items()]}


def execute_job(job_id):
    job = load_job(job_id)
    stop = threading.Event()
    mutex = threading.RLock()
    def save(**updates):
        with mutex:
            job.update(updates,updated_at=now(),heartbeat_at=now())
            atomic_json(job_path(job_id),job)
    def heartbeat():
        while not stop.wait(10): save()
    def progress(event):
        changes = {'phase':event.get('phase','roles' if job['recipe']=='roles' else 'running'),
                   'progress':{k:event.get(k) for k in ('site','page_status','name')}}
        if event.get('run_id'): changes['report_id'] = valid_id(event['run_id'])
        save(**changes)
    save(state='running',pid=os.getpid(),phase='starting')
    thread = threading.Thread(target=heartbeat,daemon=True)
    thread.start()
    try:
        if job.get('requested_by',job['created_by']) not in settings.knowledge_admin_user_ids:
            raise WikiError('任务创建账号已无管理权限')
        with UpdateLock(ROOT/'execution'):
            if job['recipe']=='inspect':
                result = snapshot()
                save(state='partial' if result['planning_error_count'] else 'completed',phase='finished',
                     result={'summary':result['counts'],
                             'issues':result['pending_pages'],'impact_reviews':result['impact_reviews']})
            else:
                store = WikiStore(WIKI_PATH,create=False)
                if job['recipe']=='roles':
                    from llmwiki.guide_coverage import load_catalog,refresh_catalog
                    from llmwiki.collector import Collector
                    from llmwiki.latest_guides import sync_latest
                    catalog = CACHE/'mcguide-catalog.json'
                    data = load_catalog(catalog) if job.get('report_id') else refresh_catalog(Collector(CACHE,interval=6),catalog)
                    report = sync_latest(store,KNOWLEDGE_PATH,data,root=WIKI_PATH.parent/'latest-guides',
                        cache_directory=CACHE,resume=job.get('report_id'),progress=progress,interval=6)
                else:
                    site = 'official' if job['recipe']=='official' else 'bwiki'
                    report = run_update(store,directory=WIKI_PATH.parent/'updates',cache_directory=CACHE,
                        options=UpdateOptions(sites=(site,),roles=(),limit=4 if job['recipe']=='periods' else 3,interval=6,max_pages=12),
                        resume=job.get('report_id'),collect=job['recipe']!='build',progress=progress)
                save(state=report['state'],phase='finished',report_id=report['run_id'])
    except BaseException as exc:
        save(state='interrupted' if isinstance(exc,(KeyboardInterrupt,SystemExit)) else 'failed',phase='stopped',
             error='更新资源正忙，请稍后恢复' if isinstance(exc,UpdateBusy) else '更新未完成；请检查来源状态或恢复任务')
    finally:
        stop.set();thread.join(timeout=2)
        save(finished_at=now())


if __name__ == '__main__':
    execute_job(valid_id(sys.argv[1]))
