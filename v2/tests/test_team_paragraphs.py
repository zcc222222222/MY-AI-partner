"""Literal team headings narrow opaque paragraph flows without mixing teams."""
from copy import deepcopy
import unittest
import test_rotation_conditions as fixtures
from llmwiki import WikiError
from llmwiki.guide_curation import rotation_parts,matching_flows


class TeamParagraphTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.RotationConditionTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)

    def fixture(self):
        specs=[];rows=[]
        for i,names in enumerate([['角色甲','角色乙','角色丙'],['角色甲','角色丁','角色丙'],['角色甲','角色丁','角色戊']]):
            title='【'+' & '.join(names)+'】';lines=[title,'角色甲EQR 切 '+names[1],names[1]+'REQ 切 '+names[2],names[2]+'进场直到协奏满 开始循环']
            rows+=lines
            specs.append({'id':str(i),'format':'source-blocks','mode':'原文手法','chain_scope':'原文未标明链数','context':[],
                          'team_variant':{'label':title,'quote':title,'selectors':[title],'members':names},'phases':[title],
                          'blocks':[{'phase':title,'prose':True,'lines':lines,'cycle_reference':{'kind':'start_condition_only','quote':lines[-1]}}]})
        return '\n'.join(rows),specs

    def test_member_order_and_punctuation_do_not_select_another_team(self):
        raw,specs=self.fixture();self.fx.install(raw,specs)
        for query,index in [('角色丁和角色丙 手法',1),('角色戊+角色丁 完整手法',2),('角色乙 手法',0)]:
            pages=self.fx.fx.search(query)
            segs=[s for p in pages for s in p['guide_context']['rotation_segments']]
            self.assertEqual([s['phase'] for s in segs],specs[index]['phases'],query)
            self.assertEqual(segs[0]['team_members'],specs[index]['team_variant']['members'])
        self.assertEqual(len(self.fx.fx.search('完整手法')[0]['guide_context']['rotation_segments']),3)
        result=self.fx.fx.search(specs[2]['phases'][0])[0]
        self.assertEqual([s['phase'] for s in result['guide_context']['rotation_segments']],specs[2]['phases'])
        self.assertNotIn('角色甲EQR',result['snippet'])
        self.assertIn('角色甲EQR',result['matches'][0]['text'])

    def test_shared_member_retains_both_alternatives_but_impossible_combination_has_none(self):
        _,specs=self.fixture();data={'rotations':[{'spec':s} for s in specs]}
        self.assertEqual(len(matching_flows(data,'角色丙 手法')),2)
        self.assertEqual(matching_flows(data,'角色乙 角色戊 手法'),[])
        raw,_=self.fixture();self.fx.install(raw,specs)
        for page in self.fx.fx.search('角色乙 角色戊 完整手法'):
            context=page['guide_context'];self.assertFalse(context['rotation_segments'])
            self.assertIn('uncovered_team_request',context)

    def test_budget_drops_whole_team_without_exposing_another_teams_loop(self):
        raw,specs=self.fixture();self.fx.install(raw,specs)
        expected='\n'.join(specs[1]['blocks'][0]['lines'])
        for budget in (1000,2000,4000,6000):
            pages=self.fx.fx.store.search('角色甲 角色丁 角色丙 完整手法',game_version='documents-v1',reviewed_only=True,max_chars=budget,
                document_filters={'category':'guide','entity':'character:1','query_entity_name':'角色甲','as_of':'2026-10-24T00:00:00+00:00'})
            for page in pages:
                context=page['guide_context']
                for seg in context['rotation_segments']:
                    self.assertEqual(seg['team_members'],specs[1]['team_variant']['members'])
                    self.assertIn(expected,next(m['text'] for m in page['matches'] if m['claim_id']==seg['claim_id']))
                self.assertTrue(all(r['quote']==specs[1]['blocks'][0]['cycle_reference']['quote'] for r in context.get('cycle_references',[])))

    def test_source_heading_cannot_be_invented_or_used_as_chain_or_cycle_proof(self):
        raw,specs=self.fixture()
        for kind in ('missing','duplicate','substring','wrong_title'):
            bad=deepcopy(specs[0]);variant=bad['team_variant']
            if kind=='missing':variant['members'][1]='不存在'
            elif kind=='duplicate':variant['members'][1]=variant['members'][0]
            elif kind=='substring':variant['members'][1]='角色'
            else:variant['quote']=' & '.join(variant['members'])
            with self.assertRaises(WikiError,msg=kind):rotation_parts(raw,bad)
        self.fx.install(raw,specs)
        for query in ('角色乙 完整循环轴','角色丁 零链 手法'):
            for page in self.fx.fx.search(query):self.assertFalse(page['guide_context']['rotation_segments'])

    def test_cycle_start_wording_preserves_entering_state_without_expansion(self):
        raw,specs=self.fixture();spec=specs[0];block=spec['blocks'][0];old=block['lines'][-1]
        block['lines'][-1]='开始进入循环状态';block['cycle_reference']['quote']=block['lines'][-1]
        part=rotation_parts(raw.replace(old,block['lines'][-1]),spec)[0]
        self.assertFalse(part['cycle_reference']['expanded']);self.assertIn('开始进入循环状态',part['raw'])


if __name__=='__main__':unittest.main()
