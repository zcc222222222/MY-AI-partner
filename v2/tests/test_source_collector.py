"""Offline checks for crawl refusals, pacing, dates and immutable source revisions."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from llmwiki.collector import Collector, CollectionError, robots_policy, mcguide_document, import_document, DOCUMENT_SCOPE, html_text
from llmwiki import WikiStore


class CollectorTests(unittest.TestCase):
    def test_published_matrix_timer_does_not_capture_sidebar_or_calendar_dates(self):
        from llmwiki.sources import bwiki_endgame_sections,bwiki_doc
        heading='<div style="margin:10px;font-weight:600"><div></div>'
        html=heading+'终焉矩阵</div><p>本期增幅回路。</p>'
        html+='<span class="eventTimer" data-start="2026/10/7 04:00" data-end="2026/11/12 03:59" data-info="活动"></span>'
        html+='<p>侧栏</p><span class="eventTimer" data-start="2026/9/30 11:00" data-end="2026/10/22 10:00"></span>'
        sections=bwiki_endgame_sections(html)
        self.assertEqual(len(sections),1)
        doc=bwiki_doc('首页/'+sections[0][0],sections[0][1])
        self.assertEqual(len(doc['periods']),1)
        self.assertEqual(doc['periods'][0]['start'],'2026/10/7 04:00')
        self.assertNotIn('侧栏',doc['readable_text'])

    def test_missing_mode_timer_cannot_borrow_the_next_modes_dates(self):
        from llmwiki.sources import bwiki_endgame_sections
        heading='<div style="margin:10px;font-weight:600"><div></div>'
        html=heading+'冥歌海墟</div><p>没有周期</p>'+heading+'终焉矩阵</div>'
        html+='<span class="eventTimer" data-start="2026/10/7 04:00" data-end="2026/11/12 03:59"></span>'
        with self.assertRaises(CollectionError):bwiki_endgame_sections(html)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.t = 100000.0
        self.starts = []

    def sleep(self, seconds):
        self.t += seconds

    def client(self):
        return Collector(self.tmp.name, interval=0, clock=lambda: self.t, sleep=self.sleep)

    def curl(self, status=200, body=b'{}', extra=''):
        def run(args, **kwargs):
            self.starts.append(self.t)
            Path(args[args.index('-D') + 1]).write_text(f'HTTP/1.1 {status}\r\n{extra}', encoding='latin-1')
            Path(args[args.index('-o') + 1]).write_bytes(body)
            return SimpleNamespace(stdout=str(status).encode(), returncode=0)
        return run

    def test_real_bwiki_wildcards_and_specific_agent(self):
        text = 'User-agent: *\nDisallow: /*index.php?*\nDisallow: /*File:*\nDisallow: /*%E7%89%B9%E6%AE%8A:*\nCrawl-delay: 8\n'
        self.assertEqual(robots_policy(text, '/wutheringwaves/首页'), (True, 8))
        for path in ['/wutheringwaves/index.php?title=x', '/wutheringwaves/File:icon.png', '/wutheringwaves/特殊:最近更改']:
            self.assertFalse(robots_policy(text, path)[0])
        text += 'User-agent: MY-AIPARTNER\nDisallow: /\nAllow: /public$\n'
        self.assertTrue(robots_policy(text, '/public')[0])
        self.assertFalse(robots_policy(text, '/public/extra')[0])

    def test_pacing_cache_and_pacing_across_new_process_instance(self):
        c = self.client()
        with patch('llmwiki.collector.subprocess.run', side_effect=self.curl()):
            c.fetch('https://guide-server.aki-game.com/a')
            c.fetch('https://guide-server.aki-game.com/a')
            self.client().fetch('https://guide-server.aki-game.com/b')
        self.assertEqual(len(self.starts), 3)  # robots, a, b (cached robots)
        self.assertTrue(all(b-a >= 5 for a,b in zip(self.starts, self.starts[1:])))

    def test_429_pauses_future_runs_without_retrying(self):
        c = self.client()
        c.robots['guide-server.aki-game.com'] = ''
        with patch('llmwiki.collector.subprocess.run', side_effect=self.curl(429, extra='Retry-After: 120\r\n')):
            with self.assertRaises(CollectionError):
                c.fetch('https://guide-server.aki-game.com/a')
        with patch('llmwiki.collector.subprocess.run') as run:
            with self.assertRaises(CollectionError):
                self.client().fetch('https://guide-server.aki-game.com/b')
            run.assert_not_called()

    def test_disallowed_paths_never_reach_network(self):
        c = self.client()
        c.robots['wiki.biligame.com'] = 'User-agent: *\nDisallow: /*index.php?*'
        with patch('llmwiki.collector.subprocess.run') as run:
            for url in ['https://wiki.biligame.com/wutheringwaves/index.php?title=x', 'https://example.org/a']:
                with self.assertRaises(CollectionError):
                    c.fetch(url)
            run.assert_not_called()

    def test_author_date_is_not_patch_and_counts_do_not_change_revision(self):
        row = {'id': 1, 'modifiedAt': 1783597526}
        detail = {'id': 1, 'likeCount': 2, 'baseTexts': [{'language': 'zh-Hans', 'introductionName': '攻略', 'introductionSource': '作者'}]}
        doc = mcguide_document(row, detail)
        self.assertIsNone(doc['game_version'])
        self.assertEqual(doc['date_basis'], 'platform_modifiedAt')
        store = WikiStore(Path(self.tmp.name) / 'wiki.db')
        one = import_document(store, 'mcguide/1', doc, origin='https://mcguide.kurogames.com/')
        detail['likeCount'] += 100
        two = import_document(store, 'mcguide/1', mcguide_document(row, detail), origin='https://mcguide.kurogames.com/')
        self.assertFalse(two['changed'])
        row['modifiedAt'] += 86400
        three = import_document(store, 'mcguide/1', mcguide_document(row, detail), origin='https://mcguide.kurogames.com/')
        self.assertEqual(three['revision'], 2)
        self.assertEqual(store.search('攻略', game_version='3.7'), [])
        self.assertEqual(store.search('攻略', game_version=DOCUMENT_SCOPE), [])  # source is not an approved page
        self.assertEqual(store.get_source('mcguide/1', game_version=DOCUMENT_SCOPE, revision=1)['metadata']['source_date'], doc['source_date'])

    def test_plain_text_preserves_image_attributes_and_skips_scripts(self):
        text = html_text('<p>推荐属性：<img alt="热熔"></p><script>do_bad_thing()</script><p>仅限零链</p>')
        self.assertIn('热熔', text)
        self.assertIn('仅限零链', text)
        self.assertNotIn('do_bad_thing', text)


if __name__ == '__main__':
    unittest.main()
