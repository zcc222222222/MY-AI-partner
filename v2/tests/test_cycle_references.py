"""Unexpanded loop references stay source text, never synthesized rotations."""
from copy import deepcopy
import unittest
import test_rotation_conditions as fixtures
from llmwiki import WikiError
from llmwiki.guide_curation import rotation_parts


class CycleReferenceTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.RotationConditionTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)

    def fixture(self):
        notation='（e-共鸣技能，E-强化共鸣技能）'
        raw='开局角色甲EAA，切角色乙ER，切角色甲等协奏结束开始循环。'
        spec={'id':'ref','format':'source-blocks','mode':'原文手法','chain_scope':'原文未标明链数',
              'context':[notation],'notation':{'quote':notation,'case_sensitive':True},'phases':['原文流程'],
              'blocks':[{'phase':'原文流程','prose':True,'lines':[raw],
                         'sequence_anchors':['开局角色甲EAA','切角色乙ER','切角色甲等协奏结束'],
                         'cycle_reference':{'kind':'start_condition_only','quote':'等协奏结束开始循环'}}]}
        return notation+'\n'+raw,spec

    def test_generic_flow_keeps_wait_and_case_definitions_atomically(self):
        raw,spec=self.fixture();self.fx.install(raw,[spec])
        result=self.fx.fx.search('完整手法')[0]
        segment=result['guide_context']['rotation_segments'][0]
        self.assertTrue(segment['notation_case_sensitive'])
        self.assertFalse(segment['cycle_reference']['expanded'])
        text=next(m['text'] for m in result['matches'] if m['claim_id']==segment['claim_id'])
        self.assertIn(spec['context'][0],text)
        self.assertIn('等协奏结束开始循环',text)

    def test_expanded_cycle_request_cannot_reuse_opening(self):
        raw,spec=self.fixture();self.fx.install(raw,[spec])
        for query in ('完整循环轴','展开循环 手法','闭合循环 手法'):
            for page in self.fx.fx.search(query):
                context=page['guide_context'];self.assertFalse(context['rotation_segments'])
                self.assertIn('unexpanded_cycle_request',context)
                self.assertTrue(all(not r['included_in_claim'] for r in context['cycle_references']))

    def test_fabricated_reference_or_notation_definition_rejected(self):
        raw,spec=self.fixture()
        for kind in ('quote','kind','case','definition'):
            bad=deepcopy(spec)
            if kind=='quote':bad['blocks'][0]['cycle_reference']['quote']='立即循环'
            elif kind=='kind':bad['blocks'][0]['cycle_reference']['kind']='repeat_opening'
            elif kind=='case':bad['notation']['case_sensitive']=False
            else:bad['notation']['quote']='e-技能'
            with self.assertRaises(WikiError,msg=kind):rotation_parts(raw,bad)

    def test_literal_wheelchair_axis_title_routes_to_rotation(self):
        raw,spec=self.fixture();block=spec['blocks'][0]
        block.update(phase='三火轮椅轴',lines=['三火轮椅轴',block['lines'][0]])
        del block['sequence_anchors'];spec['phases']=['三火轮椅轴']
        self.fx.install(raw.splitlines()[0]+'\n'+'\n'.join(block['lines']),[spec])
        pages=self.fx.fx.search('三火轮椅轴')
        self.assertEqual([s['phase'] for p in pages for s in p['guide_context']['rotation_segments']],['三火轮椅轴'])

    def test_partial_ellipsis_never_becomes_a_closed_cycle(self):
        raw,spec=self.fixture();block=spec['blocks'][0]
        block['lines'][0]+='切角色乙....'
        block['cycle_reference']={'kind':'partial_ellipsis','quote':'切角色乙....'}
        raw=spec['context'][0]+'\n'+block['lines'][0]
        self.fx.install(raw,[spec])
        for page in self.fx.fx.search('完整循环轴'):
            context=page['guide_context'];self.assertFalse(context['rotation_segments'])
            self.assertIn('unexpanded_cycle_request',context)
            self.assertEqual(context['cycle_references'][0]['kind'],'partial_ellipsis')


if __name__=='__main__':unittest.main()
