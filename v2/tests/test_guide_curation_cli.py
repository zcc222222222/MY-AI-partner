"""Batch preparation must fail before writing drafts; reruns must stay idempotent."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import unittest
import test_guide_curation as fixtures
from wiki_test_helpers import publish_reviewed

ROOT=Path(__file__).resolve().parents[1]


class CurationCommandTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.CurationTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.folder=Path(self.fx.fx.tmp.name)

    def manifest(self,name,**changes):
        data=deepcopy(self.fx.manifest);data['page_id']=self.fx.page['page_id'];data.update(changes)
        path=self.folder/(name+'.json');path.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8');return path

    def run_cli(self,*manifests):
        return subprocess.run([sys.executable,'-X','utf8',str(ROOT/'scripts/curate-guide.py'),*map(str,manifests),
                               '--db',str(self.fx.store.path),'--knowledge-db',str(self.fx.kb),'--stage',
                               '--output',str(self.folder/'report.json')],capture_output=True,text=True,encoding='utf-8')

    def count_drafts(self):
        with self.fx.store.connection() as c:return c.execute('SELECT count(*) FROM drafts').fetchone()[0]

    def test_published_manifest_rerun_creates_no_draft_or_backup(self):
        manifest=self.manifest('one');first=self.run_cli(manifest);self.assertEqual(first.returncode,0,first.stderr)
        report=json.loads((self.folder/'report.json').read_text(encoding='utf-8'))
        publish_reviewed(self.fx.store,report['draft']['draft_id'])
        count=self.count_drafts();backups=list((self.folder/'backups').iterdir())
        repeat=self.run_cli(manifest);self.assertEqual(repeat.returncode,0,repeat.stderr)
        report=json.loads((self.folder/'report.json').read_text(encoding='utf-8'))
        self.assertTrue(report['unchanged']);self.assertFalse(report['staged'])
        self.assertEqual(count,self.count_drafts());self.assertEqual(backups,list((self.folder/'backups').iterdir()))

    def test_stale_second_manifest_prevents_any_batch_staging(self):
        second=deepcopy(self.fx.page);second['page_id']='page/second'
        publish_reviewed(self.fx.store,self.fx.store.stage_page(second)['draft_id'])
        good=self.manifest('good');stale=self.manifest('stale',page_id='page/second',source_revision=999)
        count=self.count_drafts();result=self.run_cli(good,stale)
        self.assertNotEqual(result.returncode,0);self.assertIn('标注清单已过期',result.stderr)
        self.assertEqual(count,self.count_drafts());self.assertFalse((self.folder/'backups').exists())


if __name__=='__main__':unittest.main()
