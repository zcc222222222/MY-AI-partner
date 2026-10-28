"""Actual adapter imports and response shapes; no public network requests."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from llmwiki import WikiStore
from llmwiki.sources import collect_mcguide, collect_bwiki, collect_official
from llmwiki.collector import DOCUMENT_SCOPE


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = WikiStore(Path(self.tmp.name) / 'wiki.db')
        self.report = {'imported': [], 'skipped': [], 'errors': []}

    def test_mcguide_full_detail_import_and_repeat_are_idempotent_empty_chinese_titles_skipped(self):
        requested, checkpoints = [], []
        class Client:
            def json(_self, url):
                requested.append(url)
                if url.endswith('/role/avatar/list'):
                    data = [{'roleGbId': '1210'}]
                elif '/introduction/list?' in url:
                    data = [{'id': 1, 'modifiedAt': 1783597526, 'texts': [{'language':'zh-Hans', 'introductionName':'攻略'}]},
                            {'id': 2, 'texts': [{'language':'zh-Hans', 'introductionName':''}]}]
                else:
                    self.assertTrue(url.endswith('roleGbId=1210&id=1'))
                    data = {'id': 1, 'baseTexts':[{'language':'zh-Hans', 'introductionName':'攻略', 'introductionSource':'作者',
                                                 'introductionDetail':'<p>聚爆模态推荐队伍甲。</p>'}]}
                return {'code':200, 'data':data}
        for _ in range(2):
            collect_mcguide(Client(), self.store, ['1210'], 5, self.report, progress=checkpoints.append)
        self.assertEqual(len(checkpoints), 2)
        self.assertTrue(self.report['imported'][0]['changed'])
        self.assertFalse(self.report['imported'][1]['changed'])
        self.assertEqual(self.store.get_source('mcguide/1210/1', game_version=DOCUMENT_SCOPE)['revision'], 1)
        self.assertFalse(any('id=2' in u for u in requested))

    def test_official_menu_selection_fetches_full_article_not_listing_excerpt(self):
        class Client:
            def json(_self, url):
                if url.endswith('ArticleMenu.json'):
                    return [{'articleId': 1,'articleTitle':'3.7版本内容说明','startTime':'2026-09-30 10:00:00','articleContent':'短摘录'}]
                return {'articleId':1,'articleTitle':'3.7版本内容说明','startTime':'2026-09-30 10:00:00','articleContent':'<p>完整公告，测试内容。</p>'}
        collect_official(Client(), self.store, 1, self.report, progress=lambda _:None)
        text = self.store.get_source('official/news/1', game_version=DOCUMENT_SCOPE)['text']
        self.assertIn('完整公告', text)
        self.assertNotIn('短摘录', text)

    def test_bwiki_cycles_remain_separate_and_calendar_uses_public_subpage(self):
        requested = []
        class Client:
            def fetch(_self, url):
                requested.append(url)
                if url.endswith('首页'):
                    marker = '<div style="margin:10px;font-weight:600"><div></div>'
                    html = ('"wgRevisionId":123,' + marker + '逆境深塔</div>测试塔'
                            '<span data-start="2026/10/12 04:00" data-end="2026/11/9 03:59"></span><hr>'
                            + marker + '冥歌海墟</div>测试海墟'
                            '<span data-start="2026/9/28 04:00" data-end="2026/10/26 03:59"></span><hr>')
                else:
                    html = '<div class="mw-parser-output"><p>「测试活动」</p><p>2026/10/1 04:00~2026/10/31 03:59</p><!-- NewPP limit report'
                return html.encode(), {}, 200
        collect_bwiki(Client(), self.store, 3, self.report, progress=lambda _:None)
        self.assertEqual(len(self.report['imported']), 3)
        self.assertNotEqual(self.report['imported'][0]['periods'], self.report['imported'][1]['periods'])
        self.assertTrue(requested[-1].endswith('首页/活动日历'))

    def test_mcguide_catalog_entry_without_chinese_guides_is_explicit_gap(self):
        class Client:
            def json(_self, url):
                if url.endswith('/role/avatar/list'):
                    return {'code':200,'data':[{'roleGbId':'1'}]}
                return {'code':200,'data':[{'id':1,'texts':[{'language':'en','introductionName':'Guide'}]}]}
        collect_mcguide(Client(),self.store,['1'],1,self.report,progress=lambda _:None)
        self.assertEqual(self.report['imported'],[])
        self.assertEqual(self.report['skipped'],[{'role':'1','reason':'no_chinese_guides_in_public_listing'}])

    def test_requested_official_id_missing_from_menu_is_reported_as_a_gap(self):
        class Client:
            def json(_self, url):
                self.assertTrue(url.endswith('ArticleMenu.json'))
                return []
        collect_official(Client(), self.store, 1, self.report, [999], progress=lambda _:None)
        self.assertEqual(self.report['imported'], [])
        self.assertEqual(self.report['skipped'][0]['article_id'], 999)


if __name__ == '__main__':
    unittest.main()
