"""Browser lifetime, request coalescing and approved-period stale-if-error boundaries."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from app.agents import live_guides as live
from app.agents.community_browser import BrowserSession
from app.agents.community_worker import BrowserWorker
from app.agents.guide_snapshots import GuideSnapshots, MAX_AGE
from app.services import retrieval_diagnostics as diagnostics, chat_service


SCHEDULE = {'start':'2026-10-12T04:00:00+08:00','end':'2026-11-09T03:59:00+08:00',
            'label_date':'2026-10-12','source_id':'bwiki/首页/逆境深塔','source_revision':1,
            'document_version':'test-reviewed-revision','schedule_verified':True}
STAMP = datetime(2026,10,27,tzinfo=timezone.utc).timestamp()
POST = {'gameId':3,'gameForumId':12,'postStatus':0,'reviewStatus':1,'postId':'1559169379056631808',
        'postTitle':'逆境深塔261012期配队攻略','userName':'测试作者','createTimestamp':1791763200000,
        'topicList':[],'postContent':'公开搜索摘要'}
DATA = {'status':'ok','posts':[POST],'sort':'latest','records_seen':1}


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.now=[STAMP]
        self.client=live.PublicCatalog(clock=lambda:self.now[0],sleep=lambda n:None,cache_directory=self.temp.name)
        self.addCleanup(self.client.close)
        self.client.snapshots=GuideSnapshots(Path(self.temp.name)/'periods',clock=lambda:self.now[0])
        self.addCleanup(patch.stopall)
        patch.object(live,'CLIENT',self.client).start()
        patch.object(live,'current_period',return_value=SCHEDULE).start()
        patch.object(live,'current_day',return_value='2026-10-27').start()
        patch.object(live.time,'time',side_effect=lambda:self.now[0]).start()
        patch.object(self.client,'_policy').start()
        self.network=patch.object(self.client.browser,'search',return_value=deepcopy(DATA)).start()
        self.state={};self.token=diagnostics.OBSERVATION.set(self.state)
        self.addCleanup(diagnostics.OBSERVATION.reset,self.token)

    def seed(self):
        result=live.find_gameplay_guides('本期深塔攻略')
        self.assertEqual(result['status'],'links_found')
        self.now[0]+=21601  # Normal six-hour search metadata expired; period snapshot remains.
        self.network.return_value={'status':'source_unavailable','failure_stage':'search_response','error_type':'TimeoutError'}
        return result

    def test_refresh_failure_reuses_current_period_and_labels_capture_time(self):
        self.seed()
        result=live.find_gameplay_guides('本期深塔攻略')
        self.assertEqual(result['status'],'links_found')
        self.assertTrue(result['currency_verified'])
        self.assertEqual(result['freshness']['refresh_failure_reason'],'timeout')
        self.assertIn('2026-10-27',result['freshness']['captured_at'])
        self.assertIn('刷新未成功',result['results'][0]['snapshot_notice'])
        self.assertEqual(self.state['reason'],'timeout')
        self.assertEqual(self.state['cache'],'disk')
        self.assertEqual(diagnostics.classify(result,self.state),'timeout')
        self.assertEqual(self.network.call_count,2)
        # A concurrent/follow-up request during cooldown uses the same approved snapshot.
        again=live.find_gameplay_guides('本期深塔攻略')
        self.assertEqual(again['status'],'links_found');self.assertEqual(self.network.call_count,2)

    def test_fallback_keeps_author_version_and_stage_filters(self):
        self.seed()
        result=live.find_gameplay_guides('本期深塔攻略',author='其他作者')
        self.assertEqual(result['results'],[])
        self.assertEqual(live.find_gameplay_guides('本期深塔攻略',game_version='9.9')['results'],[])
        self.assertEqual(live.find_gameplay_guides('本期深塔第三层攻略')['results'],[])

    def test_force_refresh_and_historical_as_of_do_not_hide_failure(self):
        self.seed()
        token=diagnostics.FORCE_REFRESH.set(True)
        try:self.assertEqual(live.find_gameplay_guides('本期深塔攻略')['status'],'source_unavailable')
        finally:diagnostics.FORCE_REFRESH.reset(token)
        self.assertEqual(live.find_gameplay_guides('本期深塔攻略',as_of='2026-10-27')['status'],'source_unavailable')

    def test_expired_snapshot_or_revised_schedule_not_used(self):
        self.seed();self.now[0]=STAMP+MAX_AGE
        self.assertEqual(live.find_gameplay_guides('本期深塔攻略')['status'],'source_unavailable')
        self.now[0]=STAMP+22000
        with patch.object(live,'current_period',return_value={**SCHEDULE,'source_revision':2}):
            self.assertEqual(live.find_gameplay_guides('本期深塔攻略')['status'],'source_unavailable')
        with patch.object(live,'current_period',return_value=None):
            self.assertEqual(live.find_gameplay_guides('本期深塔攻略')['status'],'source_unavailable')

    def test_restrictions_robots_and_changed_structure_are_not_suppressed(self):
        for value in ({'status':'access_restricted'},
                      {'status':'source_unavailable','failure_reason':'robots_disallowed'},
                      {'status':'source_unavailable','error_type':'ValueError','failure_stage':'search_response'}):
            with self.subTest(value=value):
                self.client.cache.clear();self.client.next_at=0;self.state.clear()
                self.client.snapshots.save('逆境深塔',SCHEDULE,DATA)
                self.network.return_value=value
                token=diagnostics.FORCE_REFRESH.set(True)
                try:live.find_gameplay_guides('本期深塔攻略')
                finally:diagnostics.FORCE_REFRESH.reset(token)
                # Keep normal search metadata absent, testing the next failed refresh too.
                result=live.find_gameplay_guides('本期深塔攻略')
                self.assertEqual(result['results'],[])

    def test_timestamp_does_not_slide_on_memory_hit_and_no_private_fields_saved(self):
        self.network.return_value={**deepcopy(DATA),'private_query':'secret',
                                   'posts':[{**POST,'cookies':'secret'}]}
        live.find_gameplay_guides('本期深塔攻略')
        self.now[0]+=100
        live.find_gameplay_guides('本期深塔攻略')
        saved=self.client.snapshots.load('逆境深塔',SCHEDULE)
        self.assertEqual(saved['captured_at'],STAMP)
        content=next((Path(self.temp.name)/'periods').glob('*.json')).read_text(encoding='utf-8')
        self.assertNotIn('secret',content)
        self.client.snapshots.save('逆境深塔',SCHEDULE,{**DATA,'captured_at':STAMP-1})
        self.assertEqual(self.client.snapshots.load('逆境深塔',SCHEDULE)['captured_at'],STAMP)

    def test_corrupt_future_and_cross_period_snapshots_rejected(self):
        store=self.client.snapshots
        store.save('逆境深塔',SCHEDULE,DATA)
        identity=store.identity('逆境深塔',SCHEDULE);path=store.path(identity)
        value=json.loads(path.read_text(encoding='utf-8'));value['captured_at']=STAMP+1
        path.write_text(json.dumps(value),encoding='utf-8');self.assertIsNone(store.load('逆境深塔',SCHEDULE))
        path.write_text('{invalid',encoding='utf-8');self.assertIsNone(store.load('逆境深塔',SCHEDULE))
        self.now[0]=datetime(2026,11,9,4,tzinfo=timezone.utc).timestamp()
        self.assertIsNone(store.load('逆境深塔',SCHEDULE))

    def test_api_robots_denial_does_not_fall_back_without_diagnostic_context(self):
        self.client.snapshots.save('逆境深塔',SCHEDULE,DATA)
        self.client.robots=Mock();self.client.robots.can_fetch.return_value=False
        self.client.robots_until=STAMP+1000
        # Restore the real policy method, rather than a transport stub reporting failure.
        token=diagnostics.OBSERVATION.set(None)
        try:
            with patch.object(self.client,'_policy',side_effect=lambda path:live.PublicCatalog._policy(self.client,path)):
                result=live.find_gameplay_guides('本期深塔攻略')
            self.assertEqual(result['results'],[])
        finally:diagnostics.OBSERVATION.reset(token)

    def test_capture_notice_is_appended_even_if_model_omits_it(self):
        notice='本次刷新未成功，展示当期已保存资料，采集于2026-10-27 08:00。'
        suffix=chat_service._source_suffix([{'source_kind':'live_kurobbs','presentation':'article',
                       'author':'作者','date_label':'2026-10-12','snapshot_notice':notice}], '回答')
        self.assertIn(notice,suffix)


class BrowserLifetimeTests(unittest.TestCase):
    def test_real_worker_protocol_reuses_process_and_recovers_after_timeout(self):
        # Real subprocess and stdin/stdout protocol, with local transport fixture; no external requests.
        with tempfile.TemporaryDirectory() as directory:
            script=Path(directory)/'worker.py'
            script.write_text('import sys,json,os,time\nfor line in sys.stdin:\n q=json.loads(line)\n if q["keyword"]=="slow": time.sleep(4)\n if q["keyword"]=="invalid":\n  print("not JSON",flush=True)\n  continue\n print(json.dumps({"pid":os.getpid(),"status":"ok","posts":[]}),flush=True)\n',encoding='utf-8')
            original=subprocess.Popen
            def popen(args,**kwargs):return original([sys.executable,'-u',str(script)],**kwargs)
            worker=BrowserWorker(timeout=.3)
            try:
                with patch('app.agents.community_worker.subprocess.Popen',side_effect=popen):
                    first=worker.search('逆境深塔');second=worker.search('终焉矩阵')
                    self.assertEqual(first['pid'],second['pid'])
                    with self.assertRaises(subprocess.TimeoutExpired):worker.search('slow')
                    recovered=worker.search('冥歌海墟')
                    self.assertNotEqual(first['pid'],recovered['pid'])
                    with self.assertRaises(ValueError):worker.search('invalid')
                    self.assertIsNone(worker.process)
            finally:worker.close()
            self.assertIsNone(worker.process)

    def test_anonymous_browser_context_reused_and_rebuilt_if_disconnected(self):
        session=BrowserSession();runtime=Mock();browser=runtime.chromium.launch.return_value
        browser.is_connected.return_value=True
        page=browser.new_context.return_value.new_page.return_value;page.is_closed.return_value=False
        with patch('playwright.sync_api.sync_playwright') as start:
            start.return_value.start.return_value=runtime
            self.assertIs(session.ensure_page(),session.ensure_page())
            self.assertEqual(runtime.chromium.launch.call_count,1)
            browser.is_connected.return_value=False
            session.ensure_page();self.assertEqual(runtime.chromium.launch.call_count,2)
        session.close()
        self.assertIsNone(session.page)

    def test_concurrent_same_topic_is_searched_only_once(self):
        client=live.PublicCatalog();entered=threading.Event();release=threading.Event()
        def search(keyword):entered.set();release.wait(3);return deepcopy(DATA)
        try:
            with patch.object(client,'_policy'),patch.object(client.browser,'search',side_effect=search) as fetch:
                with ThreadPoolExecutor(max_workers=6) as pool:
                    futures=[pool.submit(client.community,'逆境深塔') for _ in range(6)]
                    self.assertTrue(entered.wait(2));release.set()
                    values=[f.result(timeout=5) for f in futures]
                self.assertEqual(fetch.call_count,1)
                values[0]['posts'].clear();self.assertTrue(values[1]['posts'])
        finally:client.close()

    def test_queue_capacity_is_bounded(self):
        client=live.PublicCatalog()
        try:
            for _ in range(8):self.assertTrue(client.community_slots.acquire(False))
            with patch.object(client.browser,'search') as search:
                with self.assertRaises(ValueError):client.community('逆境深塔')
                search.assert_not_called()
        finally:
            for _ in range(8):client.community_slots.release()
            client.close()


if __name__=='__main__':unittest.main()
