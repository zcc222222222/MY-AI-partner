"""Source stage roles and alternatives must not manufacture a full rotation."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch
import test_rotation_conditions as fixtures
from llmwiki import WikiError
from llmwiki.guide_curation import rotation_parts


class RotationStructureTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.RotationConditionTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)

    def staged_fixture(self):
        blocks=[{'phase':'起手阶段','phase_role':'opening','lines':['起手阶段：角色乙A→E，满协奏再切人']},
                {'phase':'增伤阶段','phase_role':'amplification','lines':['增伤阶段：角色丙E→R，退场前声骸']},
                {'phase':'爆发阶段','phase_role':'burst','lines':['爆发阶段：角色甲R→A→切角色丙']},
                {'phase':'充能循环','phase_role':'cycle','lines':['充能循环：角色乙Q→A→E，武器可减少1次空中攻击'] }]
        spec={'id':'team','format':'source-blocks','mode':'原文手法','chain_scope':'原文未标明链数',
              'context':[],'phases':[b['phase'] for b in blocks],'blocks':blocks}
        return '\n'.join(b['lines'][0] for b in blocks),spec

    def branch_fixture(self):
        rows=['①【格挡】→【格挡反击】→过程中被攻击→【强化反击】。',
              '②【格挡】→【格挡反击】→未被攻击→【普攻第二段】。']
        spec={'id':'parry','format':'source-blocks','mode':'原文手法','chain_scope':'原文未标明链数',
              'coverage_scope':'local_moves','context':[],'phases':['格挡'],
              'blocks':[{'phase':'格挡','lines':rows,'branches':[
                  {'line':rows[0],'condition':'过程中被攻击','outcome':'【强化反击】'},
                  {'line':rows[1],'condition':'未被攻击','outcome':'【普攻第二段】'}]}]}
        return '\n'.join(rows),spec

    def test_full_stage_order_and_canonical_opening_cycle_keep_source_labels(self):
        raw,spec=self.staged_fixture();self.fx.install(raw,[spec])
        page=self.fx.fx.search('完整手法')[0]
        self.assertEqual([s['phase'] for s in page['guide_context']['rotation_segments']],spec['phases'])
        narrowed=self.fx.fx.search('启动轴 循环轴')[0]
        self.assertEqual([s['phase'] for s in narrowed['guide_context']['rotation_segments']],['起手阶段','充能循环'])
        delivered='\n'.join(m['text'] for m in narrowed['matches'])
        self.assertIn('满协奏再切人',delivered);self.assertIn('减少1次空中攻击',delivered)
        self.assertNotIn('角色丙E→R',delivered)
        burst=self.fx.fx.search('爆发阶段 手法')[0]
        self.assertEqual([s['phase'] for s in burst['guide_context']['rotation_segments']],['爆发阶段'])
        all_named=self.fx.fx.search('输出流程 起手 增伤 爆发 充能循环')[0]
        self.assertEqual([s['phase'] for s in all_named['guide_context']['rotation_segments']],spec['phases'])

    def test_role_must_match_literal_stage_name(self):
        raw,spec=self.staged_fixture()
        for role in ('cycle','invented'):
            bad=deepcopy(spec);bad['blocks'][0]['phase_role']=role
            with self.assertRaises(WikiError):rotation_parts(raw,bad)

    def test_full_flow_precedes_incidental_gear_under_claim_cap(self):
        from llmwiki.guide_selection import select_claims
        from llmwiki.search import terms
        raw,spec=self.staged_fixture();page=self.fx.install(raw,[spec])
        rotation=[c for c in page['claims'] if c['claim_id'].startswith('curation-rotation-')]
        gear=[{**deepcopy(rotation[0]),'claim_id':name,'text':text} for name,text in [('weapon','武器推荐甲'),('echo','声骸推荐乙')]]
        headings={c['claim_id']:'输出流程' for c in rotation};headings.update(weapon='武器',echo='声骸')
        for query in ('完整手法 武器 声骸','输出流程 起手 增伤 爆发 充能循环 武器 声骸'):
            selected,_=select_claims(page,gear+rotation,headings,query,terms(query))
            self.assertEqual([c['claim_id'] for c in selected[:4]],[c['claim_id'] for c in rotation])

    def test_both_branches_preserved_as_alternatives_and_not_full_axis(self):
        raw,spec=self.branch_fixture();self.fx.install(raw,[spec])
        page=self.fx.fx.search('格挡反击')[0]
        seg=page['guide_context']['rotation_segments'][0]
        self.assertEqual(seg['coverage_scope'],'local_moves')
        self.assertEqual([b['outcome'] for b in seg['branches']],['【强化反击】','【普攻第二段】'])
        self.assertIn(raw,page['matches'][0]['text'])
        for query in ('完整输出轴','整套循环手法','启动轴 循环轴'):
            result=self.fx.fx.search(query);self.assertTrue(result,query)
            self.assertFalse(result[0]['guide_context']['rotation_segments'])
            self.assertIn('uncovered_local_rotation_request',result[0]['guide_context'])
            self.assertNotIn('【强化反击】','\n'.join(m['text'] for m in result[0]['matches']))

    def test_branch_conditions_cannot_be_swapped_omitted_or_reordered(self):
        raw,spec=self.branch_fixture()
        for kind in ('swap','omit','reorder','scope'):
            bad=deepcopy(spec);refs=bad['blocks'][0]['branches']
            if kind=='swap':refs[0]['outcome']='【普攻第二段】'
            elif kind=='omit':refs.pop()
            elif kind=='reorder':refs.reverse()
            else:bad['coverage_scope']='whole_rotation'
            with self.assertRaises(WikiError,msg=kind):rotation_parts(raw,bad)

    def test_budget_drops_whole_branch_block_not_half_an_alternative(self):
        raw,spec=self.branch_fixture();self.fx.install(raw,[spec])
        for budget in (1000,1800,3000,5000):
            pages=self.fx.fx.store.search('角色甲 格挡反击',game_version='documents-v1',reviewed_only=True,max_chars=budget,
                     document_filters={'category':'guide','entity':'character:1','query_entity_name':'角色甲','as_of':'2026-10-24T00:00:00+00:00'})
            for page in pages:
                for seg in page['guide_context']['rotation_segments']:
                    self.assertEqual(len(seg['branches']),2)
                    self.assertIn(raw,next(m['text'] for m in page['matches'] if m['claim_id']==seg['claim_id']))

    def test_tampered_branch_metadata_fails_publish_lint(self):
        raw,spec=self.branch_fixture();page=self.fx.install(raw,[spec])
        page['guide_curation']['rotations'][0]['segments'][0]['branches'][0]['outcome']='伪造动作'
        self.assertFalse(self.fx.fx.store.stage_page(page)['valid'])

    def test_final_tool_budget_includes_trace_before_dropping_pages(self):
        from llmwiki import tools
        raw,spec=self.staged_fixture();self.fx.install(raw,[spec])
        with patch.object(tools,'WIKI_PATH',self.fx.fx.store.path):
            with patch.object(tools,'MAX_RESULT_CHARS',50000):
                full=tools.search_documents('角色甲 完整手法',category='guide',as_of='2026-10-24')
            self.assertTrue(full['results'])
            limit=len(json.dumps(full,ensure_ascii=False))-10
            with patch.object(tools,'MAX_RESULT_CHARS',limit):
                bounded=tools.search_documents('角色甲 完整手法',category='guide',as_of='2026-10-24')
            self.assertNotIn('error',bounded)
            self.assertLessEqual(len(json.dumps(bounded,ensure_ascii=False)),limit)
            self.assertEqual(bounded['trace']['selected'],[])
            self.assertEqual(bounded['results'],[])


if __name__=='__main__':unittest.main()
