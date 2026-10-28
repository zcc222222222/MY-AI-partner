"""Provided links are neither fetched articles nor approved official periods."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.agents import gameplay_references as refs, live_guides as live, orchestrator
from app.services.chat_service import _source_suffix


class GameplayReferencesTests(unittest.TestCase):
    def lookup(self, query='本期冥歌海墟攻略', **kwargs):
        return refs.lookup(query, **({'as_of': '2026-10-28'} | kwargs))

    def test_provided_link_has_explicit_provenance_and_no_claimed_content(self):
        result = self.lookup()
        self.assertEqual(result['status'], 'reference_available')
        self.assertEqual(result['presentation'], 'links')
        self.assertFalse(result['currency_verified'])
        self.assertFalse(result['body_read'])
        self.assertFalse(result['automatic_refresh'])
        row = result['results'][0]
        self.assertEqual(row['url'], 'https://weibo.com/7908192515/RoCN7wee6')
        self.assertEqual(row['images'], [])
        self.assertIsNone(row['post_date'])
        self.assertNotIn('text_excerpt', row)

    def test_date_boundaries_and_expiration_are_conservative(self):
        for moment in ('2026-10-25', '2026-10-26', '2026-11-23', '2026-11-24'):
            self.assertIsNone(self.lookup(as_of=moment), moment)
        self.assertIsNotNone(self.lookup(as_of='2026-11-22'))

    def test_author_version_and_publication_filters_are_not_ignored(self):
        self.assertIsNotNone(self.lookup(author='@漂泊帮帮团', game_version='3.8'))
        for params in ({'author': '其他作者'}, {'game_version': '3.6'}, {'updated_on': '2026-10-27'}):
            self.assertIsNone(self.lookup(**params))

    def test_other_topics_sites_historical_periods_and_unread_floor_scope_do_not_match(self):
        for query in ('本期深塔攻略', '海墟和矩阵攻略', '上一期海墟攻略', '海墟260928期攻略',
                      '2026年9月28日海墟攻略', '海墟11层攻略', '只查询库街区海墟攻略', '海墟攻略不要微博只查库街区'):
            self.assertIsNone(self.lookup(query), query)
        self.assertIsNotNone(self.lookup('海墟261026期攻略'))
        self.assertIsNotNone(self.lookup('3.7海墟攻略'))

    def test_unavailable_registry_does_not_fail_primary_tool(self):
        with patch.object(refs, 'PATH', Path('missing-reference-file.json')):
            self.assertIsNone(self.lookup())

    def test_arbitrary_urls_cannot_be_registered_as_weibo_posts(self):
        data = json.loads(refs.PATH.read_text(encoding='utf-8'))
        data['entries'][0]['url'] = 'https://example.invalid/phishing'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'refs.json'
            path.write_text(json.dumps(data), encoding='utf-8')
            with patch.object(refs, 'PATH', path):
                self.assertIsNone(self.lookup())

    def test_fallback_retains_failure_cause_and_does_not_pretend_images_ready(self):
        with patch.object(live, '_find_gameplay_guides', return_value={'status': 'source_unavailable', 'failure_reason': 'timeout', 'results': []}):
            result = live.find_gameplay_guides('海墟攻略', as_of='2026-10-28')
        self.assertEqual(result['status'], 'reference_available')
        self.assertEqual(result['primary_lookup']['failure_reason'], 'timeout')
        self.assertEqual(result['presentation'], 'links')
        self.assertEqual(result['results'][0]['content_status'], 'reference_only')

    def test_existing_verified_hits_keep_priority(self):
        primary = {'status': 'links_found', 'results': [], 'currency_verified': True}
        with patch.object(live, '_find_gameplay_guides', return_value=primary), patch.object(refs, 'lookup') as lookup:
            result = live.find_gameplay_guides('海墟攻略', presentation='links', as_of='2026-10-28')
        lookup.assert_not_called()
        self.assertTrue(result['currency_verified'])

    def test_registered_link_reaches_answer_sources(self):
        payload = self.lookup()
        def stream(*args, **kwargs):
            if kwargs.get('tools'):
                yield {'type': 'tool_calls', 'tool_calls': [{'id': 'ref', 'function': {'name': 'find_gameplay_guides', 'arguments': '{}'}}]}
                yield {'type': 'completion', 'finish_reason': 'tool_calls'}
            else:
                yield {'type': 'delta', 'content': '请查看原帖。'}
                yield {'type': 'completion', 'finish_reason': 'stop'}
        # Avoid repeating the tool in round two by ending after the first trace.
        with patch.object(orchestrator, 'stream_completion', side_effect=stream), patch.object(orchestrator, 'execute_tool', return_value=json.dumps(payload)):
            agent = orchestrator.run_agent([{'role': 'user', 'content': '海墟攻略'}], tools=live.TOOLS)
            try:
                trace = next(event for event in agent if event['type'] == 'knowledge_trace')
            finally:
                agent.close()
        self.assertEqual(trace['sources'][0]['source_kind'], 'provided_weibo_reference')
        suffix = _source_suffix(trace['sources'], '')
        self.assertIn('https://weibo.com/7908192515/RoCN7wee6', suffix)
        self.assertIn('正文未读取', suffix)


if __name__ == '__main__':
    unittest.main()
