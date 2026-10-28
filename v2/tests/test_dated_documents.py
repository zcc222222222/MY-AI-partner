"""Temporal and version safety of approved source-backed document retrieval."""
from copy import deepcopy
import json
from contextlib import closing
import sqlite3
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from llmwiki import WikiStore
from llmwiki.collector import DOCUMENT_SCOPE, import_document
from llmwiki.documents import document_parts, iso_time
from llmwiki.compiler import compile_page, CompileOptions
from llmwiki import tools
from app.agents import orchestrator, tools as agent_tools
from app.services.chat_service import _source_suffix
from wiki_test_helpers import publish_reviewed


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = WikiStore(Path(self.tmp.name) / 'wiki.db')

    def source(self, sid='events', text=None, **extra):
        doc = {'schema': 'dated-source-v1', 'kind': 'community_event_or_endgame', 'title': '活动日历', 'author': 'BWIKI贡献者',
               'source_date': None, 'date_basis': 'not_provided', 'game_version': None, 'document_version': 'fixture-v1',
               'readable_text': text or '「过去活动」\n2026/9/1 04:00~2026/9/2 03:59\n「当前活动」\n2026/10/1 04:00~2026/10/31 03:59\n「未来活动」\n2026/11/1 04:00~2026/11/30 03:59', **extra}
        import_document(self.store, sid, doc, origin='https://wiki.biligame.com/wutheringwaves/首页')
        return doc

    def compile(self, sid='events', part=0):
        def transport(messages, budget):
            data = json.loads(messages[1]['content'])
            claims = [{'text': e['text'].replace('\n', ' '), 'evidence_ids': [e['evidence_id']]} for e in data['evidence']]
            return {'content': json.dumps({'sections': [{'heading': '正文', 'claims': claims}], 'open_questions': [], 'unresolved_conflicts': []}, ensure_ascii=False),
                    'finish_reason': 'stop', 'usage': {'completion_tokens': 100}}
        report = compile_page(self.store, 'page/' + sid + str(part), game_version=DOCUMENT_SCOPE, kind='reference',
                              entities=['document:' + sid], topics=[], document_part=part,
                              transport=transport, runs_dir=Path(self.tmp.name) / 'runs')
        self.assertTrue(report['valid'], report)
        return report

    def search(self, query, **filters):
        return self.store.search(query, game_version=DOCUMENT_SCOPE, reviewed_only=True,
                                 document_filters={'as_of': iso_time('2026-10-17'), **filters})

    def test_current_events_exclude_future_and_expired_before_claim_selection(self):
        self.source()
        report = self.compile()
        self.assertEqual(self.search('当前活动'), [])
        publish_reviewed(self.store, report['draft_id'])
        current = self.search('活动')
        text = ''.join(m['text'] for p in current for m in p['matches'])
        self.assertIn('当前活动', text)
        self.assertNotIn('过去活动', text)
        self.assertNotIn('未来活动', text)
        self.assertEqual(self.search('未来活动', as_of=iso_time('2026-12-17')), [])
        future = self.search('未来活动', as_of=iso_time('2026-11-17'))
        self.assertIn('未来活动', future[0]['snippet'])

    def test_scope_metadata_and_claim_time_cannot_be_tampered(self):
        self.source()
        report = self.compile()
        with self.store.connection() as c:
            data = json.loads(c.execute('SELECT payload FROM drafts WHERE draft_id=?', (report['draft_id'],)).fetchone()[0])
        for change in ('version', 'period', 'citation'):
            tampered = deepcopy(data)
            if change == 'version':
                tampered['document_metadata']['applicable_game_version'] = '3.7'
            elif change == 'period':
                tampered['claims'][1]['valid_periods'] = []
            else:
                tampered['claims'][1]['evidence_ids'] = ['E001']
            self.assertFalse(self.store.stage_page(tampered)['valid'], change)

    def test_skipped_claim_keeps_other_claims_bound_to_their_own_periods(self):
        self.source()
        report = self.compile()
        publish_reviewed(self.store, report['draft_id'])
        original = self.search('活动', include_inactive=True)[0]
        self.assertGreaterEqual(len(original['matches']), 2)
        omitted = original['matches'][0]['claim_id']
        expected = {m['claim_id']: m['valid_periods'] for m in original['matches']}
        from llmwiki.payload import citations_for
        with patch('llmwiki.search.citations_for', side_effect=lambda page, claim: [] if claim['claim_id'] == omitted else citations_for(page, claim)):
            result = self.search('活动', include_inactive=True)[0]
        self.assertNotIn(omitted, [m['claim_id'] for m in result['matches']])
        for match in result['matches']:
            self.assertEqual(match['valid_periods'], expected[match['claim_id']])

    def test_expired_endgame_is_not_found_via_its_metadata_claim(self):
        self.source(title='逆境深塔', text='深境干扰：敌方热熔抗性降低10%。',
                    periods=[{'start': '2026/10/12 04:00', 'end': '2026/11/9 03:59'}])
        report = self.compile()
        publish_reviewed(self.store, report['draft_id'])
        self.assertTrue(self.search('热熔'))
        self.assertEqual(self.search('热熔', as_of=iso_time('2026-11-10')), [])
        self.assertEqual(self.search('BWIKI', as_of=iso_time('2026-11-10')), [])

    def test_unknown_patch_never_matches_explicit_patch_and_future_source_hidden(self):
        self.source(title='官方公告', kind='official_notice', text='活动期间开放新玩法。', source_date='2026-10-01 12:00:00', date_basis='official_startTime')
        report = self.compile()
        publish_reviewed(self.store, report['draft_id'])
        self.assertTrue(self.search('玩法'))
        self.assertEqual(self.search('玩法', applicable_game_version='3.7'), [])
        self.assertEqual(self.search('玩法', as_of=iso_time('2026-09-30')), [])

    def test_source_update_withdraws_old_document_until_reviewed(self):
        self.source()
        report = self.compile()
        publish_reviewed(self.store, report['draft_id'])
        self.assertTrue(self.search('当前活动'))
        self.source(document_version='fixture-v2')
        self.assertEqual(self.search('当前活动'), [])

    def test_guide_projection_uses_only_chinese_author_narrative_and_separates_dates(self):
        self.source(sid='guide', kind='author_guide', title='作者攻略', source_date='2025-08-18T17:34:33+08:00', date_basis='platform_modifiedAt',
                    content={'baseTexts': [{'language': 'en', 'introductionDetail': 'untranslated secret'},
                                           {'language': 'zh-Hans', 'introductionDetail': '<p>作者推荐组合A；仅在聚爆模式使用。</p>'}],
                             'roleSkill': {'raw': '无关的巨大技能面板'}})
        report = self.compile('guide')
        publish_reviewed(self.store, report['draft_id'])
        found = self.search('聚爆')
        self.assertIn('作者攻略观点：', found[0]['snippet'])
        self.assertNotIn('无关', found[0]['snippet'])
        self.assertIsNone(found[0]['game_version'])
        self.assertEqual(found[0]['document_metadata']['date_basis'], 'platform_modifiedAt')

    def test_tool_scope_and_payload_are_bounded(self):
        self.source()
        publish_reviewed(self.store, self.compile()['draft_id'])
        with patch.object(tools, 'WIKI_PATH', self.store.path):
            result = tools.search_documents('活动', category='event', as_of='2026-10-17')
            self.assertEqual(result['status'], 'found')
            self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), tools.MAX_RESULT_CHARS)
            self.assertEqual(tools.search_documents('活动', game_version='documents-v1')['error'], 'invalid_version')
            self.assertIn('error', tools.search_documents('活动', as_of='yesterday'))
            self.assertEqual(tools.search_documents('活动', game_version='3.8', as_of='2026-10-17')['status'], 'no_reviewed_match')

    def test_end_time_keeps_server_timezone_and_includes_the_last_minute(self):
        self.assertEqual(iso_time('2026/11/11 11:59', end=True), '2026-11-11T03:59:59.999999+00:00')

    def test_chat_trace_and_footer_preserve_document_dates_and_license(self):
        self.source()
        publish_reviewed(self.store, self.compile()['draft_id'])
        call = {'id': 'doc1', 'type': 'function', 'function': {'name': 'search_documents', 'arguments': json.dumps({'query': '活动', 'as_of': '2026-10-17'})}}
        snapshots = []
        def stream(messages, **kwargs):
            snapshots.append(deepcopy(messages))
            if len(snapshots) == 1:
                return iter([{'type': 'tool_calls', 'tool_calls': [call]}, {'type': 'completion', 'finish_reason': 'tool_calls'}])
            return iter([{'type': 'delta', 'content': '当前活动可参加。'}, {'type': 'completion', 'finish_reason': 'stop'}])
        original = [{'role': 'system', 'content': 'unchanged-persona'}, {'role': 'user', 'content': '活动'}]
        with patch.object(tools, 'WIKI_PATH', self.store.path), patch.object(orchestrator, 'stream_completion', side_effect=stream):
            events = list(orchestrator.run_agent(original, tools=agent_tools.TOOLS))
        trace = next(e for e in events if e['type'] == 'knowledge_trace')
        self.assertEqual(trace['trace']['route'], 'documents')
        self.assertEqual(snapshots[1][0], original[0])
        self.assertEqual(snapshots[1][-2]['role'], 'tool')
        self.assertEqual(snapshots[1][-1]['content'], orchestrator.DOCUMENT_RESPONSE_RULES)
        self.assertNotIn('documents-v1', _source_suffix(trace['sources'], ''))
        source = {'title': '活动日历', 'url': 'https://wiki.biligame.com/wutheringwaves/首页', 'game_version': None,
                  'date_label': '更新时间未提供', 'license_note': 'CC BY-NC-SA 4.0'}
        footer = _source_suffix([source], source['url'])
        self.assertIn('鸣潮BWIKI贡献者', footer)
        self.assertIn('CC BY-NC-SA 4.0', footer)

    def test_document_explicit_patch_also_locks_followup_wiki_and_sql(self):
        calls = [{'id': str(i), 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}} for i, (name, args) in enumerate([
            ('search_documents', {'query': '攻略', 'game_version': '3.8'}),
            ('search_wiki', {'query': '机制', 'entity_name': '角色'}),
            ('get_character_skills', {'name': '角色'})])]
        snapshots, executed = [], []
        def stream(messages, **kwargs):
            snapshots.append(deepcopy(messages))
            if len(snapshots) == 1:
                return iter([{'type': 'tool_calls', 'tool_calls': calls}, {'type': 'completion', 'finish_reason': 'tool_calls'}])
            return iter([{'type': 'delta', 'content': '该版本资料不足。'}, {'type': 'completion', 'finish_reason': 'stop'}])
        def execute(name, arguments):
            executed.append((name, json.loads(arguments)))
            return json.dumps({'results': [], 'status': 'no_reviewed_match', 'game_version': '3.8'})
        with patch.object(orchestrator, 'stream_completion', side_effect=stream), patch.object(orchestrator, 'execute_tool', side_effect=execute), patch.object(orchestrator, '_structured_version', return_value='3.7'):
            list(orchestrator.run_agent([{'role': 'user', 'content': '3.8版本攻略'}], tools=agent_tools.TOOLS))
        self.assertEqual(executed[1][1]['game_version'], '3.8')
        self.assertFalse(any(name == 'get_character_skills' for name, _ in executed))

    def test_named_future_event_returns_schedule_instead_of_misleading_absence(self):
        self.source()
        publish_reviewed(self.store, self.compile()['draft_id'])
        with patch.object(tools, 'WIKI_PATH', self.store.path):
            future = tools.search_documents('未来', category='event', as_of='2026-10-17')
            self.assertEqual(future['status'], 'found')
            self.assertEqual(future['time_mode'], 'schedule_lookup_including_inactive')
            matched = [m for p in future['results'] for m in p['matches']]
            self.assertEqual(matched[0]['time_status'], 'not_started')
            self.assertTrue(matched[0]['valid_periods'][0]['start'].endswith('+08:00'))
            active = tools.search_documents('活动', category='event', as_of='2026-10-17')
            self.assertEqual(active['time_mode'], 'active_or_undated')
            self.assertNotIn('未来活动', ''.join(p['snippet'] for p in active['results']))
            named = tools.search_documents('未来活动 活动时间', category='event', as_of='2026-10-17')
            self.assertEqual(named['time_mode'], 'named_event_schedule')
            self.assertTrue(all(m['time_status'] == 'not_started' for p in named['results'] for m in p['matches']))

    def test_author_and_update_date_are_separate_from_query_time(self):
        self.source(kind='official_notice', title='版本公告', text='活动时间另行公告。', author='作者甲',
                    source_date='2026-10-17T20:00:00+08:00', date_basis='platform_modifiedAt')
        publish_reviewed(self.store, self.compile()['draft_id'])
        with patch.object(tools, 'WIKI_PATH', self.store.path):
            self.assertEqual(tools.search_documents('活动', author='作者甲', updated_on='2026-10-17', as_of='2026-10-17')['status'], 'found')
            self.assertEqual(tools.search_documents('活动', author='作者乙', as_of='2026-10-17')['status'], 'no_reviewed_match')
            self.assertEqual(tools.search_documents('活动', updated_on='2026-10-16', as_of='2026-10-17')['status'], 'no_reviewed_match')

    def test_same_element_rover_queries_keep_both_ids_and_exclude_other_characters(self):
        self.source(sid='rover', title='气动攻略', kind='author_guide', content={'baseTexts': [{'language': 'zh-Hans', 'introductionDetail': '气动配队。'}]})
        source = self.store.get_source('rover', game_version=DOCUMENT_SCOPE, include_raw=True)
        source['metadata']['entities'].append('character:1408')
        self.store.import_source('rover', game_version=DOCUMENT_SCOPE, title=source['title'], raw=source['raw'],
                                 media_type='application/json', origin=source['origin'], metadata=source['metadata'])
        publish_reviewed(self.store, self.compile('rover')['draft_id'])
        kb = Path(self.tmp.name) / 'kb.db'
        with closing(sqlite3.connect(kb)) as c, c:
            c.executescript("CREATE TABLE characters(id,name); INSERT INTO characters VALUES(1406,'漂泊者·气动'),(1408,'漂泊者·气动'),(1508,'其他角色'); CREATE TABLE aliases(entity_type,entity_id,alias);")
        with patch.object(tools, 'WIKI_PATH', self.store.path), patch.object(tools, 'KNOWLEDGE_PATH', kb):
            result = tools.search_documents('配队', category='guide', entity_name='漂泊者·气动')
            self.assertEqual(result['status'], 'found')
            self.assertEqual(result['results'][0]['document_metadata']['entity_ids'], ['character:1408'])
            for eid in ('1406','1408'):
                shared=tools.search_documents('配队',category='guide',entity_name=eid)
                self.assertEqual(shared['status'],'found')
                self.assertEqual(shared['results'][0]['document_metadata']['entity_ids'],['character:1408'])
                self.assertEqual(set(shared['trace']['entities']),{'character:1406','character:1408'})
            self.assertEqual(tools.search_documents('配队', entity_name='其他角色')['status'], 'no_reviewed_match')

    def test_rotation_word_fallback_keeps_character_author_date_and_version_filters(self):
        for sid, role in [('target', '1'), ('other', '2')]:
            doc = self.source(sid=sid, title='测试攻略', kind='author_guide', author='作者甲',
                              source_date='2026-07-29T12:00:00+08:00', date_basis='platform_modifiedAt',
                              content={'baseTexts':[{'language':'zh-Hans','introductionDetail':'示例流程为变奏入场再释放共鸣技能。'}]})
            import_document(self.store,sid,doc,origin='https://mcguide.kurogames.com/',entities=['character:'+role])
            publish_reviewed(self.store,self.compile(sid)['draft_id'])
        kb=Path(self.tmp.name)/'rotation-kb.db'
        with closing(sqlite3.connect(kb)) as c, c:
            c.executescript("CREATE TABLE characters(id,name); INSERT INTO characters VALUES(1,'角色甲'),(2,'角色乙'); CREATE TABLE aliases(entity_type,entity_id,alias);")
        with patch.object(tools,'WIKI_PATH',self.store.path),patch.object(tools,'KNOWLEDGE_PATH',kb):
            for query in ('手法','输出手法','操作手法'):
                result=tools.search_documents(query,category='guide',entity_name='角色甲',author='作者甲',updated_on='2026-07-29')
                self.assertEqual(result['status'],'found')
                self.assertEqual([p['page_id'] for p in result['results']],['page/target0'])
                self.assertIn('示例流程',result['results'][0]['snippet'])
                self.assertIn('query_expansion',result['trace'])
            for extra in ({'game_version':'3.8'},{'author':'作者乙'},{'updated_on':'2026-07-28'}):
                result=tools.search_documents('手法',category='guide',entity_name='角色甲',**extra)
                self.assertEqual(result['status'],'no_reviewed_match',extra)
            for query,category,entity in [('不存在术语','guide','角色甲'),('手法',None,'角色甲'),('手法','guide',None)]:
                result=tools.search_documents(query,category=category,entity_name=entity)
                self.assertEqual(result['status'],'no_reviewed_match')
                self.assertNotIn('query_expansion',result['trace'])
            direct=tools.search_documents('流程',category='guide',entity_name='角色甲')
            self.assertEqual(direct['status'],'found')
            self.assertNotIn('query_expansion',direct['trace'])

    def test_reviewed_repair_ancestry_preserves_unchanged_input_cache(self):
        self.source()
        compiled = self.compile()
        publish_reviewed(self.store, compiled['draft_id'])
        current = self.store.get_page('page/events0', game_version=DOCUMENT_SCOPE)
        page = current['page']
        page['base_revision'] = current['revision']
        page['compiler'].pop('input_fingerprint')
        page['semantic_review_parent'] = compiled['draft_id']
        repaired = self.store.stage_page(page)
        self.assertTrue(repaired['valid'])
        publish_reviewed(self.store, repaired['draft_id'])
        def transport(*_):
            self.fail('Unchanged reviewed source must not call the model again')
        result = compile_page(self.store, 'page/events0', game_version=DOCUMENT_SCOPE, kind='reference',
                              entities=['document:events'], topics=[], transport=transport, runs_dir=Path(self.tmp.name) / 'reuse')
        self.assertEqual(result['status'], 'skipped', result)


if __name__ == '__main__':
    unittest.main()
