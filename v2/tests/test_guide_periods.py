"""A current cycle must come from approved, current, intact absolute-time evidence."""
from datetime import datetime
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.agents.guide_periods import current_period
from app.agents.guide_periods import named_scope


class GuidePeriodTests(unittest.TestCase):
    def test_named_matrix_label_is_pinned_to_revision_and_absolute_bounds(self):
        period={'source_id':'bwiki/首页/终焉矩阵','source_revision':1,
                'document_version':'sha256/af6fe88c126cfa5e5a8b385968d30351ec40b9fcdc531696e1e556082ca9eefe',
                'start':'2026-10-07T04:00:00+08:00','end':'2026-11-12T03:59:59.999999+08:00'}
        self.assertEqual(named_scope(period)['label'],'S2第三阶段')
        self.assertIsNotNone(named_scope({**period,'source_revision':2}))
        self.assertIsNone(named_scope({**period,'document_version':'changed'}))
        self.assertIsNone(named_scope({**period,'end':'2026-12-01T03:59:59+08:00'}))
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'wiki.db'
        self.page = 'document/bwiki/首页/逆境深塔/part-1'
        self.source = 'bwiki/首页/逆境深塔'
        self.payload = {'base_revision': 0, 'document_metadata': {'category': 'endgame', 'source_id': self.source,
            'source_revision': 3, 'origin': 'https://wiki.biligame.com/wutheringwaves/首页',
            'periods': [{'start': '2026-10-11T20:00:00+00:00', 'end': '2026-11-08T19:59:59.999999+00:00'}]}}
        fp = hashlib.sha256(json.dumps({k:v for k,v in self.payload.items() if k != 'base_revision'},
            ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        with closing(sqlite3.connect(self.path)) as c, c:
            c.executescript('''CREATE TABLE pages(page_id,game_version,locale,active_revision,dirty);
                CREATE TABLE page_revisions(page_id,game_version,locale,revision,payload,fingerprint);
                CREATE TABLE sources(source_id,game_version,locale,current_revision);
                CREATE TABLE source_revisions(source_id,game_version,locale,revision,deleted);
                CREATE TABLE source_dependencies(page_id,game_version,locale,page_revision,source_id,source_revision);
                CREATE TABLE drafts(draft_id,page_id,game_version,locale,fingerprint,state);
                CREATE TABLE reviews(review_id,draft_id,fingerprint,decision);''')
            c.execute('INSERT INTO pages VALUES(?,?,?,?,?)', (self.page, 'documents-v1', 'zh', 1, 0))
            c.execute('INSERT INTO page_revisions VALUES(?,?,?,?,?,?)', (self.page, 'documents-v1', 'zh', 1, json.dumps(self.payload), fp))
            c.execute('INSERT INTO sources VALUES(?,?,?,?)', (self.source, 'documents-v1', 'zh', 3))
            c.execute('INSERT INTO source_revisions VALUES(?,?,?,?,?)', (self.source, 'documents-v1', 'zh', 3, 0))
            c.execute('INSERT INTO source_dependencies VALUES(?,?,?,?,?,?)', (self.page, 'documents-v1', 'zh', 1, self.source, 3))
            c.execute('INSERT INTO drafts VALUES(?,?,?,?,?,?)', ('d1', self.page, 'documents-v1', 'zh', fp, 'published'))
            c.execute('INSERT INTO reviews VALUES(?,?,?,?)', (1, 'd1', fp, 'approved'))

    def resolve(self, stamp='2026-10-26T12:00:00+08:00'):
        return current_period('逆境深塔', path=self.path, moment=datetime.fromisoformat(stamp))

    def test_exact_local_bounds_and_read_only(self):
        before = self.path.read_bytes()
        result = self.resolve()
        self.assertTrue(result['schedule_verified'])
        self.assertEqual(result['start'], '2026-10-12T04:00:00+08:00')
        self.assertEqual(result['label_date'], '2026-10-12')
        self.assertEqual(self.path.read_bytes(), before)

    def test_refresh_boundary_does_not_infer_next_period(self):
        self.assertIsNotNone(self.resolve('2026-11-09T03:59:59+08:00'))
        self.assertIsNone(self.resolve('2026-11-09T04:00:00+08:00'))
        self.assertIsNone(self.resolve('2026-10-12T03:59:59+08:00'))

    def test_pending_dirty_deleted_or_changed_source_not_accepted(self):
        baseline = self.path.read_bytes()
        for sql in ["UPDATE pages SET dirty=1", "UPDATE source_revisions SET deleted=1",
                    "UPDATE sources SET current_revision=4", "UPDATE reviews SET decision='rejected'",
                    "UPDATE drafts SET state='staged'", "UPDATE source_dependencies SET source_revision=2"]:
            with self.subTest(sql=sql):
                self.path.write_bytes(baseline)
                with closing(sqlite3.connect(self.path)) as c, c:
                    c.execute(sql)
                self.assertIsNone(self.resolve())

    def test_modified_payload_without_matching_review_fails_closed(self):
        self.payload['document_metadata']['periods'][0]['end'] = '2028-12-31T00:00:00+00:00'
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute('UPDATE page_revisions SET payload=?', (json.dumps(self.payload),))
        self.assertIsNone(self.resolve())


if __name__ == '__main__': unittest.main()
