"""Catalog availability never counts as reviewed guide coverage or merges gender IDs."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from llmwiki import WikiStore, WikiError
from llmwiki.collector import import_document
from llmwiki.guide_coverage import CATALOG_URL, catalog_rows, coverage, refresh_catalog, load_catalog, select_batch


def entry(role, name, peer=None):
    return {'roleGbId':str(role),'mayRoleGbId':peer,'texts':[{'language':'zh-Hans','name':name}]}


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.store=WikiStore(self.root/'wiki.db')
        self.db=self.root/'knowledge.db'
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute('CREATE TABLE characters(id INTEGER PRIMARY KEY,name TEXT,element_name TEXT)')
            c.executemany('INSERT INTO characters VALUES (?,?,?)',[(1,'测试角色','衍射'),(2,'漂泊者·湮灭','湮灭'),(3,'漂泊者·湮灭','湮灭'),(4,'漂泊者·气动','气动')])
        self.snapshot={'schema':1,'origin':CATALOG_URL,'fetched_at':'2026-10-23','payload':{'code':200,'data':[entry(1,'测试角色'),entry(2,'漂泊者·湮灭',3),entry(4,'漂泊者·气动')]}}

    def report(self): return coverage(self.store,self.db,self.snapshot)

    def add(self):
        return import_document(self.store,'mcguide/2/100',{'schema':'dated-source-v1','kind':'author_guide','title':'测试攻略','author':'作者',
            'source_date':None,'date_basis':'not_provided','game_version':None,'document_version':'test1',
            'content':{'baseTexts':[{'language':'zh-Hans','introductionDetail':'推荐配队甲。'}]}},origin='https://mcguide.kurogames.com/',entities=['character:2'])

    def test_catalog_does_not_count_as_published_or_merge_peer(self):
        report=self.report()
        self.assertEqual(report['summary']['current_records'],0)
        peer=report['characters'][2]
        self.assertEqual(peer['status'],'not_in_catalog')
        self.assertEqual(peer['catalog_entry_via_peer'],'2')
        self.assertEqual(select_batch(report),['2','4','1'])

    def test_source_import_alone_is_pending_and_prioritized(self):
        self.add()
        report=self.report()
        self.assertEqual(report['characters'][1]['status'],'pending')
        self.assertEqual(report['characters'][1]['imported_guides'],1)
        self.assertEqual(report['characters'][2]['imported_guides'],0)
        self.assertEqual(select_batch(report,1),['2'])

    def test_only_all_current_chapters_count_and_planning_errors_are_visible(self):
        self.add()
        def plan(statuses): return {'pages':[{'source_id':'mcguide/2/100','status':s} for s in statuses],'errors':[]}
        for states,current in ((['unchanged','pending'],0),(['unchanged','unchanged'],1),([],0)):
            with patch('llmwiki.guide_coverage.plan_documents',return_value=plan(states)):
                self.assertEqual(self.report()['characters'][1]['current_guides'],current)
        with patch('llmwiki.guide_coverage.plan_documents',return_value={'pages':[],'errors':[{'message':'invalid'}]}):
            self.assertEqual(len(self.report()['planning_errors']),1)

    def test_identity_mismatch_is_reported_and_never_selected(self):
        self.snapshot['payload']['data'][0]=entry(1,'其他角色')
        report=self.report()
        self.assertEqual(report['characters'][0]['status'],'catalog_identity_mismatch')
        self.assertNotIn('1',select_batch(report,5))

    def test_catalog_peer_requires_same_local_name_and_element(self):
        self.snapshot['payload']['data'][0]['mayRoleGbId']='3'
        self.snapshot['payload']['data'][1]['mayRoleGbId']=None
        self.assertIsNone(self.report()['characters'][2]['catalog_entry_via_peer'])

    def test_unknown_local_and_foreign_only_catalog_are_separate(self):
        self.snapshot['payload']['data'] += [entry(999,'新角色'),{'roleGbId':'888','texts':[{'language':'en','name':'Test'}]}]
        report=self.report()
        self.assertEqual(report['catalog_without_local_record'][0]['role_id'],'999')
        self.assertEqual(report['summary']['catalog_entries'],4)

    def test_invalid_refresh_preserves_previous_snapshot(self):
        path=self.root/'catalog.json'
        path.write_text(json.dumps(self.snapshot),encoding='utf-8')
        class Client:
            def json(self,url): return {'code':200,'data':[entry(1,'甲'),entry(1,'乙')]}
        with self.assertRaises(WikiError): refresh_catalog(Client(),path)
        self.assertEqual(load_catalog(path),self.snapshot)

    def test_plan_does_not_fetch_or_create_missing_knowledge_db(self):
        before=self.db.read_bytes()
        with patch('llmwiki.collector.Collector.fetch',side_effect=AssertionError('network forbidden')): self.report()
        self.assertEqual(self.db.read_bytes(),before)
        with self.assertRaises(sqlite3.OperationalError): coverage(self.store,self.root/'missing.db',self.snapshot)
        self.assertFalse((self.root/'missing.db').exists())

    def test_invalid_catalog_and_batch_limits(self):
        for payload in ({'code':500,'data':[]},{'code':200,'data':[entry('../2','错')]}):
            with self.assertRaises(WikiError): catalog_rows(payload)
        for size in (0,6,True):
            with self.assertRaises(WikiError): select_batch(self.report(),size)


if __name__=='__main__': unittest.main()
