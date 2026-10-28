"""Latest ordering, resumable full catalog synchronization, safe history retirement."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from llmwiki.sources import newest_chinese_guides
from llmwiki.latest_guides import sync_latest, retire_other_guides
from llmwiki.updates import UpdateOptions, run_update, source_snapshot
from llmwiki import WikiError
import test_knowledge_updates as fixtures


class LatestTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.UpdateTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.store=self.fx.store
        self.db=self.fx.root/'knowledge.db'
        with closing(sqlite3.connect(self.db)) as c,c:
            c.executescript("CREATE TABLE characters(id,name,element_name); INSERT INTO characters VALUES(1,'测试角色','衍射');")
        self.snapshot={'schema':1,'fetched_at':'2026-10-23','payload':{'code':200,'data':[{'roleGbId':'1','texts':[{'language':'zh-Hans','name':'测试角色'}]}]}}

    def run_sync(self,**kw):
        def collect(site,client,store,options,report,progress):
            self.fx.calls['collect']+=1
            report['imported'].append(self.fx.add_document('mcguide/1/2'))
        def batch(*args,**kwargs):
            return run_update(*args,**kwargs,client_factory=fixtures.FakeClient,collect_fn=collect,
                              compile_fn=self.fx.compile,review_fn=self.fx.review)
        with patch('llmwiki.latest_guides.run_update',side_effect=batch):
            return sync_latest(self.store,self.db,self.snapshot,root=self.fx.root/'full',cache_directory=self.fx.root/'cache',**kw)

    def test_timestamp_descending_unknown_last_equal_time_stable_id(self):
        def row(i,t,language='zh-Hans'): return {'id':i,'modifiedAt':t,'texts':[{'language':language,'introductionName':'攻略'}]}
        rows=[row(1,100),row(9,None),row(2,200),row(3,200),row(4,999,'en')]
        self.assertEqual([r['id'] for r in newest_chinese_guides(rows)],[3,2,1,9])
        self.assertEqual(rows[0]['id'],1)

    def test_full_run_retires_old_only_after_latest_approved_history_preserved(self):
        self.fx.add_document('mcguide/1/1')
        report=self.run_sync()
        self.assertEqual(report['state'],'completed')
        self.assertEqual(report['summary']['completed'],1)
        self.assertEqual(report['roles'][0]['retired_sources'],['mcguide/1/1'])
        self.assertEqual(set(source_snapshot(self.store)),{'mcguide/1/2'})
        with self.store.connection() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM source_revisions WHERE source_id='mcguide/1/1'").fetchone()[0],2)
        second=self.run_sync(resume=report['run_id'])
        self.assertEqual(second['state'],'completed')
        self.assertEqual(self.fx.calls['collect'],1)

    def test_cannot_retire_when_latest_missing_or_unreviewed(self):
        self.fx.add_document('mcguide/1/1')
        with self.assertRaises(WikiError): retire_other_guides(self.store,'1','mcguide/1/2')
        self.fx.add_document('mcguide/1/2')
        with self.assertRaises(WikiError): retire_other_guides(self.store,'1','mcguide/1/2')
        self.assertEqual(len(source_snapshot(self.store)),2)

    def test_regular_update_imports_only_latest_guide_even_with_larger_site_budget(self):
        class Client(fixtures.FakeClient):
            def json(_self,url):
                if url.endswith('/role/avatar/list'):
                    data=[{'roleGbId':'1'}]
                elif '/introduction/list?' in url:
                    data=[{'id':1,'modifiedAt':100,'texts':[{'language':'zh-Hans','introductionName':'旧篇'}]},
                          {'id':2,'modifiedAt':200,'texts':[{'language':'zh-Hans','introductionName':'新篇'}]}]
                else:
                    self.assertTrue(url.endswith('id=2'))
                    data={'id':2,'baseTexts':[{'language':'zh-Hans','introductionName':'新篇','introductionDetail':'推荐配队甲。'}]}
                return {'code':200,'data':data}
        result=run_update(self.store,directory=self.fx.root/'updates',cache_directory=self.fx.root/'cache',
                          options=UpdateOptions(sites=('mcguide',),roles=('1',),limit=5),client_factory=Client,
                          compile_fn=self.fx.compile,review_fn=self.fx.review)
        self.assertEqual(result['state'],'completed')
        self.assertEqual(set(source_snapshot(self.store)),{'mcguide/1/2'})
        self.assertEqual(len(result['collection']['mcguide']['imported']),1)

    def test_semantic_rejection_preserves_old_source_and_resume_does_not_regenerate(self):
        self.fx.add_document('mcguide/1/1')
        def reject(store,draft_id,**kw):
            store.review(draft_id,decision='rejected',reviewer='fixture',reviewer_type='agent',note='Artificial semantic rejection')
            return {'status':'needs_attention','draft_id':draft_id}
        self.fx.review=reject
        report=self.run_sync()
        self.assertEqual(report['roles'][0]['status'],'needs_attention')
        self.assertEqual(len(source_snapshot(self.store)),2)
        calls=self.fx.calls['compile']
        self.run_sync(resume=report['run_id'])
        self.assertEqual(self.fx.calls['compile'],calls)


if __name__=='__main__': unittest.main()
