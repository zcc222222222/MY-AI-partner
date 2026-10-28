"""Last successful public search metadata, bound to an approved current period."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import os
import uuid
import threading

MAX_AGE = 24 * 3600
TOPICS = ('逆境深塔', '冥歌海墟', '终焉矩阵')


class GuideSnapshots:
    def __init__(self, directory, *, clock=time.time):
        self.directory = Path(directory) if directory else None
        self.clock = clock
        self.lock = threading.Lock()

    def identity(self, topic, schedule):
        if topic not in TOPICS or not schedule or not schedule.get('schedule_verified'): return None
        fields = ('start', 'end', 'source_id', 'source_revision', 'document_version', 'named_scope')
        if any(not schedule.get(k) for k in fields[:-1]): return None
        now = datetime.fromtimestamp(self.clock(), timezone.utc)
        try:
            start, end = (datetime.fromisoformat(schedule[k]) for k in ('start', 'end'))
            if not start <= now <= end: return None
        except (ValueError, TypeError): return None
        return {'topic': topic, **{k:schedule.get(k) for k in fields}}

    def path(self, identity):
        if not self.directory or not identity: return None
        digest = hashlib.sha256(json.dumps(identity,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        return self.directory/(digest+'.json')

    def load(self, topic, schedule):
        identity = self.identity(topic, schedule)
        path = self.path(identity)
        if not path: return None
        try:
            if path.stat().st_size > 2_000_000: return None
            data = json.loads(path.read_text(encoding='utf-8'))
            if data['schema'] != 'current-guide-snapshot-v1' or data['period'] != identity: return None
            if not 0 <= self.clock()-data['captured_at'] < MAX_AGE: return None
            result = data['result']
            if result.get('status') != 'ok' or not isinstance(result.get('posts'),list) or not result['posts']: return None
            return {**result, 'captured_at':data['captured_at']}
        except (OSError, ValueError, TypeError, KeyError, AttributeError): return None

    def save(self, topic, schedule, result):
        with self.lock:
            self._save(topic,schedule,result)

    def _save(self, topic, schedule, result):
        identity = self.identity(topic,schedule)
        path = self.path(identity)
        if not path or result.get('status') != 'ok' or not result.get('posts'): return
        captured_at = result.get('captured_at',self.clock())
        if not isinstance(captured_at,(int,float)) or not 0 <= self.clock()-captured_at < MAX_AGE: return
        previous = self.load(topic,schedule)
        if previous and previous['captured_at'] > captured_at: return
        # Public search previews and image references only. No user query or browser state.
        fields = ('postId','postTitle','userName','gameId','gameForumId','postStatus','reviewStatus',
                  'createTimestamp','topicList','postContent','imgContent','imgCount')
        rows = [{k:p[k] for k in fields if k in p} for p in result['posts'][:100] if isinstance(p,dict)]
        payload = {'schema':'current-guide-snapshot-v1','period':identity,'captured_at':captured_at,
                   'result':{'status':'ok','posts':rows,'records_seen':len(rows),'partial':True,
                             'sort':result.get('sort'),'body_read':False}}
        if len(json.dumps(payload,ensure_ascii=False).encode()) > 2_000_000: return
        temporary = path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
        try:
            path.parent.mkdir(parents=True,exist_ok=True)
            temporary.write_text(json.dumps(payload,ensure_ascii=False,allow_nan=False),encoding='utf-8')
            os.replace(temporary,path)
            for old in sorted(path.parent.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[24:]:
                old.unlink(missing_ok=True)
        except OSError: pass  # Cache writes must not break a successful answer.
        finally:
            try: temporary.unlink(missing_ok=True)
            except OSError: pass
