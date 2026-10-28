"""Kuro public directory lookup: transport, provenance, scopes and Agent integration."""
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
from email.message import Message
import json
from pathlib import Path
import sys
import unittest
import tempfile
from unittest.mock import Mock, patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.agents import live_guides as live, intent, tools, orchestrator
from app.agents.community_browser import retry_delay

URL = 'https://www.kurobbs.com/postDetail.html?postId=1554859718222184448'
TREE = {'id': 9, 'children': [{'id': 1322, 'name': '攻略合集', 'children': [
    {'id': 1383, 'name': '玩法攻略', 'children': [
        {'id': 1645, 'name': '团团勇者大乱斗'}, {'id': 1532, 'name': '星弹奇游'},
        {'id': 1584, 'name': '团团团团转'}]},
    {'id': 1384, 'name': '角色攻略', 'children': [{'id': 1536, 'name': '爱弥斯'}]}]}]}
RECORD = {'name': '目录编辑标题', 'invalid': False, 'content': {
    'type': 'kuro-post-component', 'linkUrl': URL, 'updateAt': 1790763771440,
    'krPost': {'id': '1554859718222184448', 'postTitle': '团团勇者大乱斗 攻略',
               'postUser': 'CarlTuring', 'postTime': '2026-09-30'}},
    'wikiPostList': [{'id': '1554859718222184448', 'postTitle': '团团勇者大乱斗 攻略',
                      'userName': 'CarlTuring', 'time': '2026-09-30', 'postValid': True}]}


def page(records=None, total=None, topic=1645):
    rows = deepcopy(records if records is not None else [RECORD])
    return {'catalogPageVersion': '3.7', 'tagTree': {'id': topic},
            'results': {'records': rows, 'total': len(rows) if total is None else total}}


class Response:
    def __init__(self, data): self.body = data.encode()
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, n): return self.body[:n]


class LiveGuidesTests(unittest.TestCase):
    @contextmanager
    def fake(self, pages=None):
        pages = pages or [page()]
        def get(path, data):
            if path == live.TREE: return deepcopy(TREE)
            return deepcopy(pages[data['page']-1])
        with patch.object(live.CLIENT, 'get', side_effect=get) as mocked, patch.object(
                live.CLIENT, 'community', return_value={'status': 'ok', 'posts': []}):
            yield mocked

    def test_registry_replaces_two_old_tools_with_one(self):
        names = [t['function']['name'] for t in tools.TOOLS]
        self.assertEqual(len(names), 13)
        self.assertEqual(len(set(names)), 13)
        self.assertIn('find_gameplay_guides', names)
        for old in ('search_live_guides', 'read_live_guide'):
            self.assertNotIn(old, names)
            self.assertIn('error', json.loads(tools.execute_tool(old, '{}')))

    def test_discovered_topics_exclude_character_pages(self):
        self.assertNotIn('爱弥斯', [t['name'] for t in live.topics(TREE)])
        with self.assertRaises(ValueError): live.topics({'id': 2})

    def test_provenance_is_post_author_not_editor_and_not_page_version(self):
        with self.fake(): result = live.find_gameplay_guides('团团勇者大乱斗攻略')
        self.assertEqual(result['status'], 'links_found')
        r = result['results'][0]
        self.assertEqual((r['title'], r['author'], r['post_date'], r['url']),
                         ('团团勇者大乱斗 攻略', 'CarlTuring', '2026-09-30', URL))
        self.assertIsNone(r['game_version'])
        self.assertFalse(r['video_verified'])
        self.assertFalse(result['coverage']['body_read'])

    def test_invalid_posts_untrusted_links_and_mismatched_authors(self):
        for url in ('https://localhost/postDetail.html?postId=1554859718222184448',
                    URL + '&redirect=https://evil.test', URL.replace('https:', 'http:'),
                    'https://www.gamersky.com/handbook/202601/2071337.shtml'):
            row = deepcopy(RECORD)
            row['content']['linkUrl'] = url
            self.assertIsNone(live.record_link(row, live.topics(TREE)[0]))
        row = deepcopy(RECORD)
        row['wikiPostList'][0]['postValid'] = False
        self.assertIsNone(live.record_link(row, live.topics(TREE)[0]))
        row = deepcopy(RECORD)
        row['wikiPostList'] = []
        row['content']['krPost']['id'] = '999999999999'
        self.assertIsNone(live.record_link(row, live.topics(TREE)[0])['author'])

    def test_scope_unverified_never_substitutes_a_new_version(self):
        with self.fake(): result = live.find_gameplay_guides('3.7团团勇者大乱斗攻略', game_version='3.7')
        self.assertEqual(result['status'], 'scope_unverified')
        self.assertEqual(result['results'], [])
        self.assertEqual(result['candidates'][0]['url'], URL)
        self.assertFalse(result['currency_verified'])

    def test_explicit_post_title_version_can_match(self):
        row = deepcopy(RECORD)
        row['wikiPostList'][0]['postTitle'] = '鸣潮3.7团团勇者大乱斗攻略'
        with self.fake([page([row])]): result = live.find_gameplay_guides('团团勇者大乱斗', game_version='3.7')
        self.assertEqual(result['status'], 'links_found')

    def test_author_date_and_update_date_not_interchangeable(self):
        with self.fake():
            self.assertEqual(live.find_gameplay_guides('团团勇者大乱斗', author='其他作者')['results'], [])
            self.assertEqual(live.find_gameplay_guides('团团勇者大乱斗', as_of='2026-09-01')['results'], [])
            r = live.find_gameplay_guides('团团勇者大乱斗', updated_on='2026-09-30')
            self.assertEqual(r['status'], 'scope_unverified')

    def test_day_filter_rejects_wrong_day_and_prefers_matching_stage(self):
        rows = []
        for i in (2, 3):
            row = deepcopy(RECORD)
            row['wikiPostList'][0]['postTitle'] = f'星弹奇游 第{"二" if i == 2 else "三"}天活动攻略'
            row['content']['linkUrl'] = 'https://www.kurobbs.com/mc/post/1554859718222184448'
            rows.append(row)
        with self.fake([page(rows, topic=1532)]):
            r = live.find_gameplay_guides('星弹奇游 第3天攻略')
        self.assertEqual(len(r['results']), 1)
        self.assertIn('第三天', r['results'][0]['title'])
        self.assertIn('/mc/post/', r['results'][0]['url'])

    def test_unknown_topic_requests_clarification_without_scanning_every_page(self):
        with self.fake() as get: r = live.find_gameplay_guides('没有指定名字的活动攻略')
        self.assertEqual(r['status'], 'needs_topic')
        self.assertEqual(get.call_count, 1)

    def test_multiple_explicit_topics_not_silently_reduced_to_longest(self):
        with self.fake() as get:
            r = live.find_gameplay_guides('星弹奇游和团团勇者大乱斗攻略链接')
        self.assertEqual(r['status'], 'ambiguous_topic')
        self.assertEqual(get.call_count, 1)
        self.assertEqual(set(r['available_topics']), {'星弹奇游', '团团勇者大乱斗'})

    def community_posts(self):
        def record(label, stamp, pid='1559169379056631808', name='轩儿Xuaner'):
            return {'gameId': 3, 'gameForumId': 12, 'postStatus': 0, 'reviewStatus': 1,
                    'postId': pid, 'postTitle': f'鸣潮<em>逆境深塔</em>{label}期推荐阵容',
                    'userName': name, 'createTimestamp': str(int(datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc).timestamp()*1000)),
                    'topicList': [{'topicName': '3.7版本攻略'}]}
        return [record('261012', '2026-10-12'),
                record('260914', '2026-10-25', '1559169379056631809'),
                record('261109', '2026-11-09', '1559169379056631810')]

    @contextmanager
    def community(self, posts=None):
        with self.fake(), patch.object(live.CLIENT, 'community', return_value={
                'status': 'ok', 'posts': posts if posts is not None else self.community_posts(),
                'records_seen': 3, 'partial': True, 'sort': 'latest'}) as search, \
                patch.object(live, 'current_day', return_value='2026-10-26'), \
                patch.object(live, 'current_period', return_value=None):
            yield search

    def test_current_period_is_candidate_not_latest_post_date_or_game_version(self):
        with self.community(): r = live.find_gameplay_guides('本期深塔攻略', game_version='3.7', presentation='links')
        self.assertEqual(r['status'], 'period_unverified')
        self.assertEqual(r['results'], [])
        self.assertEqual(r['candidates'][0]['period_start'], '2026-10-12')
        self.assertFalse(r['period_resolution']['schedule_verified'])
        self.assertFalse(r['currency_verified'])
        self.assertEqual(r['candidates'][0]['author'], '轩儿Xuaner')
        self.assertNotIn('<em>', r['candidates'][0]['title'])

    def test_current_period_automatically_selects_matching_guide_and_original_images(self):
        row = self.community_posts()[0]
        row.update({'postContent': '本期深塔推荐阵容，以下是作者原图。', 'imgCount': 6,
                    'imgContent': [{'url': f'https://prod-alicdn-community.kurobbs.com/forum/image{i}.png'} for i in range(6)]})
        schedule = {'start': '2026-10-12T04:00:00+08:00', 'end': '2026-11-09T03:59:59+08:00',
                    'label_date': '2026-10-12', 'schedule_verified': True}
        with self.community([row]) as search, patch.object(live, 'current_period', return_value=schedule):
            r = live.find_gameplay_guides('深塔攻略')
        search.assert_called_once_with('逆境深塔 261012')
        self.assertEqual(r['status'], 'links_found')
        self.assertTrue(r['currency_verified'])
        self.assertEqual(r['presentation'], 'article')
        self.assertEqual(len(r['results'][0]['images']), 6)
        self.assertEqual(r['results'][0]['content_status'], 'images_ready')

    def test_current_schedule_with_no_matching_post_does_not_show_old_article(self):
        schedule = {'label_date': '2026-11-09', 'schedule_verified': True}
        with self.community(), patch.object(live, 'current_period', return_value=schedule):
            r = live.find_gameplay_guides('本期深塔攻略')
        self.assertEqual(r['results'], [])
        self.assertEqual(r['status'], 'not_found_in_community')

    def test_matrix_named_stage_is_matched_against_reviewed_window_not_post_recency(self):
        row=self.community_posts()[0]
        row.update(postTitle='[终焉矩阵S2第三阶段] BUFF选择与队伍分配详细攻略')
        old={**row,'postId':'1559169379056631809','postTitle':'终焉矩阵S2第二阶段攻略'}
        schedule={'label_date':'2026-10-07','schedule_verified':True,
                  'named_scope':{'season':2,'stage':3,'label':'S2第三阶段'}}
        with self.community([old,row]) as search,patch.object(live,'current_period',return_value=schedule):
            result=live.find_gameplay_guides('本期终焉矩阵攻略')
            mismatch=live.find_gameplay_guides('本期终焉矩阵S2第二阶段攻略')
        self.assertEqual(search.call_args.args[0],'终焉矩阵')
        self.assertEqual(result['results'][0]['named_scope'],{'season':2,'stage':3})
        self.assertTrue(result['currency_verified'])
        self.assertEqual(mismatch['results'],[])
        self.assertEqual(live.matrix_scope('终焉矩阵 S2-3'),{'season':2,'stage':3})
        self.assertIsNone(live.matrix_scope('终焉矩阵S2'))

    def test_sea_uses_common_alias_and_still_requires_exact_current_period(self):
        row=self.community_posts()[0]
        row['postTitle']='海墟261026期配队攻略'
        schedule={'label_date':'2026-10-26','schedule_verified':True}
        with self.community([row]) as search,patch.object(live,'current_period',return_value=schedule):
            result=live.find_gameplay_guides('当前冥歌海墟攻略')
        search.assert_called_once_with('海墟 261026')
        self.assertEqual(result['status'],'links_found')
        self.assertTrue(result['currency_verified'])

    def test_explicit_period_exact_match_and_date_validity(self):
        with self.community(): r = live.find_gameplay_guides('逆境深塔261012期攻略')
        self.assertEqual(r['status'], 'links_found')
        self.assertTrue(r['results'][0]['period_match'])
        self.assertEqual(len(r['results']), 1)
        self.assertIsNone(live.period_label('261332期'))
        self.assertIsNone(live.period_label('3.7版本'))

    def test_endgame_search_does_not_depend_on_wiki_catalog_availability(self):
        with self.community(), patch.object(live.CLIENT, 'get', side_effect=TimeoutError) as wiki:
            r = live.find_gameplay_guides('本期深塔攻略')
        self.assertEqual(r['status'], 'period_unverified')
        wiki.assert_not_called()

    def test_missing_requested_period_does_not_fall_back_to_old_guide(self):
        with self.community(): r = live.find_gameplay_guides('逆境深塔261026期攻略')
        self.assertEqual(r['status'], 'not_found_in_community')
        self.assertEqual(r['results'], [])

    def test_previous_period_requires_real_schedule_not_minus_28_days(self):
        with self.community(): r = live.find_gameplay_guides('上一期深塔攻略')
        self.assertEqual(r['status'], 'needs_period')
        self.assertEqual(r['results'], [])

    def test_as_of_filters_future_posts_and_periods(self):
        with self.community(): r = live.find_gameplay_guides('当前深塔攻略', as_of='2026-10-01')
        self.assertEqual(r['candidates'], [])
        self.assertNotIn('2026-11-09', r['period_resolution']['available_labels'])

    def test_explicit_author_preserved_in_community_search(self):
        with self.community(): r = live.find_gameplay_guides('本期深塔攻略', author='其他作者')
        self.assertEqual(r['status'], 'not_found_in_community')
        self.assertEqual(r['candidates'], [])

    def test_community_excludes_wrong_game_title_and_unreviewed_posts(self):
        for changes in ({'gameId': 2}, {'postStatus': 1}, {'reviewStatus': 0},
                        {'postId': 'https://evil.test'}, {'postTitle': '其他玩法攻略'}):
            row = {**self.community_posts()[0], **changes}
            self.assertIsNone(live.community_link(row, '逆境深塔'))

    def test_missing_wiki_cards_can_fall_back_to_community_metadata(self):
        row = self.community_posts()[0]
        row['postTitle'] = '团团勇者大乱斗 通关攻略'
        with self.fake([page([])]), patch.object(live.CLIENT, 'community', return_value={
                'status': 'ok', 'posts': [row]}), patch.object(live, 'current_day', return_value='2026-10-26'):
            r = live.find_gameplay_guides('团团勇者大乱斗攻略')
        self.assertEqual(r['status'], 'links_found')
        self.assertEqual(r['results'][0]['source_kind'], 'community_search')

    def test_community_worker_unavailable_and_cooldown_not_retried(self):
        client = live.PublicCatalog(clock=lambda: 0, opener=Mock())
        client.robots = Mock()
        client.robots.can_fetch.return_value = True
        client.robots_until = 1000
        with patch.object(client.browser, 'search', return_value={'status':'access_restricted'}) as run:
            with self.assertRaises(ValueError): client.community('逆境深塔')
            with self.assertRaises(ValueError): client.community('逆境深塔')
        self.assertEqual(run.call_count, 1)
        self.assertEqual(client.next_at, 600)

    def test_period_query_reuses_recent_real_broad_metadata(self):
        client = live.PublicCatalog(clock=lambda: 0, opener=Mock())
        client.cache[('community', '逆境深塔')] = (300, {'status': 'ok', 'posts': self.community_posts()})
        with patch.object(client.browser, 'search') as run:
            r = client.community('逆境深塔 261012')
        self.assertEqual(r['status'], 'ok')
        run.assert_not_called()

    def test_browser_search_timeout_has_safe_diagnostics_and_short_cooldown(self):
        client = live.PublicCatalog(clock=lambda: 0, opener=Mock())
        client.robots = Mock()
        client.robots.can_fetch.return_value = True
        client.robots_until = 1000
        payload = json.dumps({'status':'source_unavailable','failure_stage':'search_response',
                              'error_type':'TimeoutError','message':'private browser details'})
        with patch.object(client.browser, 'search', return_value=json.loads(payload)) as run:
            with self.assertRaises(ValueError): client.community('逆境深塔')
            with self.assertRaises(ValueError): client.community('逆境深塔')
        self.assertEqual(run.call_count, 1)
        self.assertEqual(client.health()['last_failure'], {'failure_stage':'search_response','error_type':'TimeoutError'})
        self.assertEqual(client.health()['wait_seconds'], 30)
        self.assertNotIn('private', json.dumps(client.health()))

    def test_public_metadata_survives_process_restart_and_expires(self):
        with tempfile.TemporaryDirectory() as directory:
            first = live.PublicCatalog(cache_directory=directory)
            data={'status':'ok','posts':self.community_posts(),'records_seen':3,'sort':'relevance'}
            with patch.object(live.time,'time',return_value=1000):
                first._save_metadata('逆境深塔 261012',data)
            restarted = live.PublicCatalog(cache_directory=directory)
            with patch.object(live.time,'time',return_value=1100), patch.object(restarted,'_policy') as policy:
                result=restarted.community('逆境深塔 261012')
            self.assertTrue(result['metadata_cache'])
            self.assertEqual(result['posts'],data['posts'])
            policy.assert_not_called()
            with patch.object(live.time,'time',return_value=23000):
                self.assertIsNone(first._saved_metadata('逆境深塔 261012'))
            first._save_metadata('私人问题：我有什么角色',data)
            self.assertEqual(len(list(Path(directory).glob('*.json'))),1)

    def test_link_only_endgame_request_does_not_fall_back_to_rule_documents(self):
        plan = {'ambiguous': False, 'status': 'parsed', 'topics': ['endgame'], 'guide_link_requested': True}
        names = {t['function']['name'] for t in intent.allowed_tools(plan, tools.TOOLS)}
        self.assertIn('find_gameplay_guides', names)
        self.assertNotIn('search_documents', names)

    def test_retry_after_respects_site_delay_and_rejects_nonfinite_values(self):
        self.assertEqual(retry_delay('1800'), 1800)
        self.assertEqual(retry_delay('nan'), 600)

    def test_multiple_periods_require_separate_queries(self):
        with self.community() as search:
            r = live.find_gameplay_guides('逆境深塔261012期和260914期攻略')
        self.assertEqual(r['status'], 'ambiguous_period')
        search.assert_not_called()

    def test_bounded_pagination_reports_partial_coverage(self):
        with self.fake([page([RECORD]*50, total=200), page([RECORD]*50, total=200)]):
            r = live.find_gameplay_guides('团团勇者大乱斗')
        self.assertEqual(r['coverage']['records_scanned'], 100)
        self.assertTrue(r['coverage']['partial'])
        self.assertEqual(len(r['results']), 1)

    def test_unavailable_and_schema_change_are_safe(self):
        with patch.object(live.CLIENT, 'get', side_effect=TimeoutError('secret internals')):
            r = live.find_gameplay_guides('团团勇者大乱斗')
        self.assertEqual(r['status'], 'source_unavailable')
        self.assertNotIn('secret', json.dumps(r))
        with self.fake([page(topic=999)]):
            self.assertEqual(live.find_gameplay_guides('团团勇者大乱斗')['status'], 'source_unavailable')

    def test_robots_requests_rate_limit_cache_and_no_credentials(self):
        now = [0.0]
        def sleep(s): now[0] += s
        opener = Mock()
        opener.open.side_effect = [Response('User-agent: *\nDisallow: /private'),
                                  Response(json.dumps({'code': 200, 'data': TREE})),
                                  Response(json.dumps({'code': 200, 'data': page()}))]
        client = live.PublicCatalog(clock=lambda: now[0], sleep=sleep, opener=opener)
        first = client.get(live.TREE, {})
        first['children'] = []
        self.assertTrue(client.get(live.TREE, {})['children'])
        client.get(live.PAGE, {'catalogueId': 1645, 'page': 1, 'limit': 50})
        self.assertGreaterEqual(now[0], 12)
        self.assertEqual(opener.open.call_count, 3)
        req = opener.open.call_args.args[0]
        self.assertFalse(any(k.lower() in ('cookie', 'authorization', 'token') for k in req.headers))

    def test_robots_disallow_and_redirect_stop(self):
        opener = Mock()
        opener.open.return_value = Response('User-agent: *\nDisallow: /wiki/')
        client = live.PublicCatalog(opener=opener)
        with self.assertRaisesRegex(ValueError, 'robots'): client.get(live.TREE, {})
        self.assertEqual(opener.open.call_count, 1)
        self.assertIsNone(live.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://localhost'))

    def test_429_sets_shared_cooldown_without_retries(self):
        headers = Message()
        headers['Retry-After'] = '900'
        opener = Mock()
        opener.open.side_effect = HTTPError(live.API, 429, 'limited', headers, None)
        client = live.PublicCatalog(clock=lambda: 0, opener=opener)
        with self.assertRaises(ValueError): client._request(live.TREE, {})
        with self.assertRaises(ValueError): client._request(live.TREE, {})
        self.assertEqual(client.next_at, 900)
        self.assertEqual(opener.open.call_count, 1)

    def test_no_arbitrary_endpoint_or_unbounded_page_fetch(self):
        client = live.PublicCatalog(opener=Mock())
        for path, args in [('https://evil.test', {}), (live.TREE, {'gameId': 2}),
                           (live.PAGE, {'catalogueId': 1645, 'page': 3, 'limit': 50})]:
            with self.assertRaises(ValueError): client.get(path, args)
        client.opener.open.assert_not_called()

    def test_route_and_enforced_query_scope(self):
        plan = {'ambiguous': False, 'status': 'parsed', 'topics': ['event'], 'entities': [],
                'game_version': '3.7', 'live_query': '团团勇者大乱斗攻略'}
        names = {t['function']['name'] for t in intent.allowed_tools(plan, tools.TOOLS)}
        self.assertIn('find_gameplay_guides', names)
        args = intent.scoped_arguments(plan, 'find_gameplay_guides', {'query': '旧问题', 'game_version': '3.0', 'author': '自造作者'})
        self.assertEqual(args['query'], plan['live_query'])
        self.assertEqual(args['game_version'], '3.7')
        self.assertNotIn('author', args)
        plan['topics'] = ['mechanism']
        self.assertNotIn('find_gameplay_guides', {t['function']['name'] for t in intent.allowed_tools(plan, tools.TOOLS)})

    def test_consecutive_day_followups_inherit_player_topic_without_old_days(self):
        raw = {'topics': ['event'], 'entity_keys': [], 'game_version': None, 'author': None,
               'updated_on': None, 'as_of': None, 'ambiguous': False}
        messages = [{'role': 'user', 'content': '给我星弹奇游第二天攻略链接'},
                    {'role': 'assistant', 'content': '作者A，3.7，2026-05-03'},
                    {'role': 'user', 'content': '第三天呢'},
                    {'role': 'assistant', 'content': '这里是链接。'},
                    {'role': 'user', 'content': '第四天呢'}]
        with patch.object(intent, 'extract', return_value=raw), patch.object(intent, 'candidates', return_value=[]):
            plan = intent.resolve_intent(messages)
        self.assertEqual(plan['status'], 'parsed')
        self.assertIn('星弹奇游', plan['live_query'])
        self.assertIn('第四天', plan['live_query'])
        self.assertNotIn('第二天', plan['live_query'])
        self.assertNotIn('第三天', plan['live_query'])
        self.assertIsNone(plan['game_version'])
        self.assertIsNone(plan['author'])
        self.assertIsNone(live.post_url(URL + '\n'))

    def test_period_followup_preserves_gameplay_without_old_current_scope(self):
        raw = {'topics': ['endgame'], 'entity_keys': [], 'game_version': None, 'author': None,
               'updated_on': None, 'as_of': None, 'ambiguous': False}
        messages = [{'role': 'user', 'content': '本期深塔攻略有吗'},
                    {'role': 'assistant', 'content': '候选作者A，261012期'},
                    {'role': 'user', 'content': '那260914期呢'}]
        with patch.object(intent, 'extract', return_value=raw), patch.object(intent, 'candidates', return_value=[]):
            plan = intent.resolve_intent(messages)
        self.assertIn('深塔', plan['live_query'])
        self.assertIn('260914期', plan['live_query'])
        self.assertNotIn('本期', plan['live_query'])
        self.assertNotIn('261012期', plan['live_query'])
        self.assertIsNone(plan['author'])

    def test_loop_returns_link_sources_and_preserves_persona_and_protocol(self):
        snapshots = []
        calls = [{'id': 'a', 'function': {'name': 'find_gameplay_guides', 'arguments': '{}'}}]
        def stream(messages, **kw):
            snapshots.append(list(messages))
            if len(snapshots) == 1:
                return iter([{'type': 'tool_calls', 'tool_calls': calls}, {'type': 'completion', 'finish_reason': 'tool_calls'}])
            return iter([{'type': 'delta', 'content': '这里有作者攻略链接，未观看视频。'}, {'type': 'completion', 'finish_reason': 'stop'}])
        with self.fake(): payload = live.find_gameplay_guides('团团勇者大乱斗')
        with patch.object(orchestrator, 'stream_completion', side_effect=stream), patch.object(orchestrator, 'execute_tool', return_value=json.dumps(payload)):
            events = list(orchestrator.run_agent([{'role': 'system', 'content': 'persona'}, {'role': 'user', 'content': '活动攻略'}], tools=tools.TOOLS, intent_routing=False))
        trace = next(e for e in events if e['type'] == 'knowledge_trace')
        self.assertEqual(trace['sources'][0]['url'], URL)
        messages = snapshots[1]
        i = next(i for i, m in enumerate(messages) if m.get('tool_calls'))
        self.assertEqual([m['role'] for m in messages[i:i+3]], ['assistant', 'tool', 'user'])
        self.assertEqual(messages[0]['content'], 'persona')


if __name__ == '__main__': unittest.main()
