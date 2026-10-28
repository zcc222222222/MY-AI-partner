"""Source-pinned range evidence and exact, non-recursive continuation excerpts."""
from copy import deepcopy
import unittest
import test_guide_curation as fixtures
from wiki_test_helpers import publish_reviewed
from llmwiki.collector import import_document
from llmwiki.guide_curation import build_page, fingerprint, rotation_parts, rotation_scope_matches
from llmwiki.rotation_conditions import chain_levels
from llmwiki import WikiError


class RotationConditionTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.CurationTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)

    def install(self,raw,specs):
        fx=self.fx;fx.doc['role_id']='1';fx.doc['document_version']='conditional'
        fx.doc['content']['baseTexts'][0]['introductionDetail']=raw
        import_document(fx.store,'g',fx.doc,origin='https://mcguide.kurogames.com/',entities=['character:1'])
        publish_reviewed(fx.store,fx.fx.compile('g')['draft_id'])
        fx.source=fx.store.get_source('g',game_version='documents-v1')
        fx.page=fx.store.get_page('page/g0',game_version='documents-v1')['page']
        fx.manifest.update(source_revision=2,source_sha256=fingerprint(fx.source),terms=[],rotations=specs)
        page=fx.build();draft=fx.store.stage_page(page);self.assertTrue(draft['valid'],draft)
        publish_reviewed(fx.store,draft['draft_id']);return page

    def dependency_fixture(self):
        start=['启动：','角色乙首发暖机→切角色甲','角色甲变奏→R','↓','角色乙结束前普攻2次→延奏角色丙']
        cycle='循环：角色丙变奏攒满协奏后切角色甲，之后与启动轮一致。'
        spec={'id':'linked','format':'source-blocks','mode':'原文手法','chain_scope':'原文未标明链数',
              'context':[],'phases':['启动','循环'],
              'blocks':[{'phase':'启动','lines':start},
                        {'phase':'循环','prose':True,'lines':[cycle],'depends_on':[{'target_phase':'启动',
                         'relation':'continues_with_suffix','anchor':'之后与启动轮一致','reuse_lines':start[2:]}]}]}
        return '\n'.join(start+[cycle]),spec

    def test_cycle_includes_only_referenced_suffix_and_preserves_original_labels(self):
        raw,spec=self.dependency_fixture();self.install(raw,[spec])
        page=self.fx.search('角色甲 只给循环轴')[0]
        self.assertEqual([s['phase'] for s in page['guide_context']['rotation_segments']],['循环'])
        text='\n'.join(m['text'] for m in page['matches'])
        self.assertIn('角色甲变奏→R\n↓\n角色乙结束前普攻2次→延奏角色丙',text)
        self.assertIn('之后与启动轮一致',text);self.assertNotIn('首发暖机',text)
        refs=page['guide_context']['rotation_segments'][0]['dependencies']
        self.assertTrue(refs[0]['included_in_claim']);self.assertEqual(refs[0]['target_phase'],'启动')
        opening=self.fx.search('启动轴')[0]
        self.assertEqual([s['phase'] for s in opening['guide_context']['rotation_segments']],['启动'])
        self.assertEqual([s['phase'] for s in self.fx.search('启动')[0]['guide_context']['rotation_segments']],['启动'])

    def test_broken_reference_wrong_suffix_or_cycle_is_rejected(self):
        raw,spec=self.dependency_fixture()
        for kind in ('missing','self','suffix','anchor','invented_relation'):
            bad=deepcopy(spec);ref=bad['blocks'][1]['depends_on'][0]
            if kind=='missing':ref['target_phase']='其他来源的启动'
            elif kind=='self':ref['target_phase']='循环'
            elif kind=='suffix':ref['reuse_lines']=ref['reuse_lines'][:-1]
            elif kind=='anchor':ref['anchor']='作者没有写过的引用'
            else:ref['anchor']='角色丙';ref['relation']='continues_with_suffix'
            with self.assertRaises(WikiError,msg=kind):rotation_parts(raw,bad)

    def test_reused_steps_are_atomic_under_tool_budget(self):
        raw,spec=self.dependency_fixture();self.install(raw,[spec])
        for budget in (700,1400,3000,5000):
            pages=self.fx.store.search('角色甲 循环轴',game_version='documents-v1',reviewed_only=True,max_chars=budget,
                                      document_filters={'category':'guide','entity':'character:1','query_entity_name':'角色甲','as_of':'2026-10-24T00:00:00+00:00'})
            for page in pages:
                for s in page['guide_context']['rotation_segments']:
                    self.assertEqual(s['phase'],'循环')
                    match=next(m for m in page['matches'] if m['claim_id']==s['claim_id'])
                    self.assertIn('角色乙结束前普攻2次→延奏角色丙',match['text'])
                    self.assertTrue(s['dependencies'][0]['source_excerpt_complete'])

    def scope_fixture(self,kind='at_least',lo=2,hi=6):
        quote={'at_least':'2链及其以上的角色甲','range':'角色甲0—3链','exact':'角色甲4链'}[kind]
        phase='高链爆发手法';raw=phase+'：'+quote+'，角色甲重击→技能→退场'
        label={'at_least':'2链及以上','range':'0—3链','exact':'4链'}[kind]
        spec={'id':'range','format':'source-blocks','mode':'原文手法','chain_scope':label,'context':[],
              'phases':[phase],'scope_labels':['高链'],
              'owner_chain_scope':{'kind':kind,'min':lo,'max':hi,'quote':quote,'owner_id':'1','name':'角色甲','source_name':'角色甲'},
              'blocks':[{'phase':phase,'lines':[raw]}]}
        return raw,spec

    def test_above_range_matches_every_covered_level_without_inferring_zero_or_one(self):
        raw,spec=self.scope_fixture();self.install(raw,[spec])
        for query in ('2链 手法','三链 手法','4链 手法','5链 手法','满链 手法','高链 手法','2链及以上 手法','3—6链 手法'):
            pages=self.fx.search(query);self.assertTrue(pages,query)
            self.assertEqual(pages[0]['guide_context']['rotation_segments'][0]['owner_chain_scope']['min'],2)
        for query in ('0链 手法','一链 手法','0—3链 手法','2链以下 手法','低链 手法','进阶 手法'):
            self.assertFalse(rotation_scope_matches(spec,query),query)
            for page in self.fx.search(query):self.assertFalse(page['guide_context']['rotation_segments'])

    def test_numeric_ranges_expand_interior_and_exact_conditions_stay_exact(self):
        self.assertEqual(chain_levels('0—3链'),{0,1,2,3})
        self.assertEqual(chain_levels('零链到三链'),{0,1,2,3})
        self.assertEqual(chain_levels('二链及以上'),{2,3,4,5,6})
        self.assertEqual(chain_levels('二链以下'),{0,1,2})
        self.assertEqual(chain_levels('不到2链'),{0,1})
        self.assertEqual(chain_levels('不低于二链'),{2,3,4,5,6})
        self.assertEqual(chain_levels('不高于3链'),{0,1,2,3})
        self.assertEqual(chain_levels('高于3链'),{4,5,6})
        self.assertEqual(chain_levels('两链'),{2})
        self.assertEqual(chain_levels('七链'),{-1})
        self.assertEqual(chain_levels('十一链'),{-1})
        raw,spec=self.scope_fixture('range',0,3);rotation_parts(raw,spec)
        self.assertTrue(rotation_scope_matches(spec,'2链 手法'))
        self.assertFalse(rotation_scope_matches(spec,'0—6链 手法'))
        raw,spec=self.scope_fixture('exact',4,4);rotation_parts(raw,spec)
        self.assertTrue(rotation_scope_matches(spec,'4链 手法'))
        self.assertFalse(rotation_scope_matches(spec,'6链 手法'))

    def test_source_evidence_cannot_be_widened_or_bound_to_teammate(self):
        raw,spec=self.scope_fixture();self.install(raw,[spec])
        for kind in ('threshold','identity','label','quote'):
            bad=deepcopy(spec)
            if kind=='threshold':bad['owner_chain_scope']['min']=1;bad['chain_scope']='1链及以上'
            elif kind=='identity':bad['owner_chain_scope']['owner_id']='2';bad['owner_chain_scope']['name']='角色乙'
            elif kind=='label':bad['scope_labels']=['低链']
            else:bad['owner_chain_scope']['quote']='角色甲1链'
            self.fx.manifest['rotations']=[bad]
            with self.assertRaises(WikiError,msg=kind):self.fx.build()

    def test_low_label_is_not_a_numeric_zero_chain_promise(self):
        high_raw,high=self.scope_fixture()
        low_raw='低链爆发手法：角色甲A→E→退场'
        low={'id':'low','format':'source-blocks','mode':'原文手法','chain_scope':'原文未标明链数','context':[],
             'scope_labels':['低链'],'phases':['低链爆发手法'],'blocks':[{'phase':'低链爆发手法','lines':[low_raw]}]}
        self.install(low_raw+'\n'+high_raw,[low,high])
        self.assertEqual([s['phase'] for s in self.fx.search('低链 手法')[0]['guide_context']['rotation_segments']],['低链爆发手法'])
        self.assertEqual([s['phase'] for s in self.fx.search('3链 手法')[0]['guide_context']['rotation_segments']],['高链爆发手法'])
        for page in self.fx.search('0链 手法'):self.assertFalse(page['guide_context']['rotation_segments'])

    def test_unlabelled_prose_cannot_be_promoted_to_a_named_phase(self):
        raw,spec=self.dependency_fixture();bad=deepcopy(spec)
        bad['blocks'][1]['phase']='原文流程';bad['phases'][1]='原文流程'
        with self.assertRaises(WikiError):rotation_parts(raw,bad)

    def test_main_dps_and_support_queries_do_not_share_incompatible_steps(self):
        raw,team=self.dependency_fixture()
        team['context']=['同奏辅助流程：不要使用普通共鸣技能']
        team['playstyle']={'label':'同奏辅助','quote':'同奏辅助流程','selectors':['同奏辅助','同奏']}
        solo_lines=['主C玩法仅作拓展：角色甲E→R','个人连段：角色甲变奏→E→A']
        solo={'id':'solo','format':'source-blocks','mode':'原文手法','chain_scope':'原文未标明链数','context':[],
              'playstyle':{'label':'主C玩法','quote':'主C玩法仅作拓展','selectors':['主C']},
              'phases':['个人连段'],'blocks':[{'phase':'个人连段','lines':solo_lines}]}
        self.install(team['context'][0]+'\n'+raw+'\n'+'\n'.join(solo_lines),[team,solo])
        dps=self.fx.search('主C 手法')[0]
        self.assertEqual([s['phase'] for s in dps['guide_context']['rotation_segments']],['个人连段'])
        support=self.fx.search('同奏辅助 循环轴')[0]
        self.assertEqual([s['phase'] for s in support['guide_context']['rotation_segments']],['循环'])
        for page in self.fx.search('主C 循环轴'):
            self.assertFalse(page['guide_context']['rotation_segments'])
            self.assertIn('uncovered_phase_request',page['guide_context'])


if __name__=='__main__':unittest.main()
