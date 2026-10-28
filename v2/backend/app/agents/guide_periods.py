"""Read already approved absolute gameplay periods; never refresh or write Wiki state."""
from contextlib import closing
from datetime import datetime, time, timezone, timedelta, date
import hashlib
import json
from pathlib import Path
import sqlite3

LOCAL = timezone(timedelta(hours=8))


def named_scope(period):
    """Curated stage aliases are pinned to the separately approved source and exact window."""
    try:
        data = json.loads(Path(__file__).with_name('gameplay_period_aliases.json').read_text(encoding='utf-8'))
        for entry in data.get('entries',[]):
            if all(entry.get(key)==period.get(key) for key in ('source_id','document_version','start','end')) and entry.get('document_version'):
                return {key:entry[key] for key in ('season','stage','label','basis')}
    except (OSError,ValueError,TypeError,KeyError): pass
    return None


def current_period(topic, as_of=None, *, path=None, moment=None):
    if topic not in ('逆境深塔', '冥歌海墟', '终焉矩阵'):
        return None
    try:
        if path is None:
            from llmwiki.tools import WIKI_PATH
            path = WIKI_PATH
        path = Path(path)
        if not path.is_file(): return None
        reference = (datetime.combine(date.fromisoformat(as_of), time.max, LOCAL) if as_of else
                     moment or datetime.now(LOCAL))
        if reference.tzinfo is None: return None
        source_id = 'bwiki/首页/' + topic
        page_id = 'document/' + source_id + '/part-1'
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute('''SELECT p.game_version,p.locale,r.payload,r.fingerprint
                FROM pages p JOIN page_revisions r ON r.page_id=p.page_id AND r.game_version=p.game_version
                  AND r.locale=p.locale AND r.revision=p.active_revision
                WHERE p.page_id=? AND p.dirty=0 AND EXISTS(
                  SELECT 1 FROM drafts d JOIN reviews v ON v.draft_id=d.draft_id AND v.fingerprint=d.fingerprint
                  WHERE d.page_id=p.page_id AND d.game_version=p.game_version AND d.locale=p.locale
                    AND d.fingerprint=r.fingerprint AND d.state='published'
                    AND v.review_id=(SELECT MAX(review_id) FROM reviews WHERE draft_id=d.draft_id)
                    AND v.decision='approved')''', (page_id,)).fetchall()
            found = []
            for row in rows:
                payload = json.loads(row['payload'])
                canonical = json.dumps({k:v for k,v in payload.items() if k != 'base_revision'},
                                       ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
                if hashlib.sha256(canonical.encode()).hexdigest() != row['fingerprint']: continue
                meta = payload.get('document_metadata', {})
                if meta.get('source_id') != source_id or meta.get('category') != 'endgame': continue
                live_source = conn.execute('''SELECT s.current_revision, r.deleted FROM sources s
                    JOIN source_revisions r ON r.source_id=s.source_id AND r.game_version=s.game_version
                      AND r.locale=s.locale AND r.revision=s.current_revision
                    WHERE s.source_id=? AND s.game_version=? AND s.locale=?''',
                    (source_id, row['game_version'], row['locale'])).fetchone()
                if not live_source or live_source['deleted'] or live_source['current_revision'] != meta.get('source_revision'): continue
                invalid = conn.execute('''SELECT 1 FROM source_dependencies d LEFT JOIN sources s
                    ON s.source_id=d.source_id AND s.game_version=d.game_version AND s.locale=d.locale
                    WHERE d.page_id=? AND d.game_version=? AND d.locale=?
                      AND d.page_revision=(SELECT active_revision FROM pages WHERE page_id=d.page_id
                        AND game_version=d.game_version AND locale=d.locale)
                      AND (s.current_revision IS NULL OR s.current_revision!=d.source_revision) LIMIT 1''',
                    (page_id, row['game_version'], row['locale'])).fetchone()
                if invalid: continue
                for period in meta.get('periods', []):
                    start, end = datetime.fromisoformat(period['start']), datetime.fromisoformat(period['end'])
                    if start.tzinfo is None or end.tzinfo is None or not start <= reference <= end: continue
                    found.append({'start': start.astimezone(LOCAL).isoformat(), 'end': end.astimezone(LOCAL).isoformat(),
                                  'label_date': start.astimezone(LOCAL).date().isoformat(),
                                  'reference_time': reference.astimezone(LOCAL).isoformat(),
                                  'basis': 'approved_local_absolute_period', 'source_id': source_id,
                                  'source_revision': meta['source_revision'], 'source_name': '鸣潮BWIKI',
                                  'document_version':meta.get('document_version'),
                                  'source_url': meta.get('origin'), 'schedule_verified': True})
            unique = {(p['start'], p['end']): p for p in found}
            if len(unique) != 1: return None
            result = next(iter(unique.values()))
            label = named_scope(result)
            if label: result['named_scope'] = label
            return result
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, OverflowError):
        return None
