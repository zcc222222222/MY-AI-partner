"""Full paragraph preservation without invented headings or executable parsing."""
from copy import deepcopy
import unittest
import test_rotation_conditions as fixtures
from llmwiki import WikiError
from llmwiki.guide_curation import rotation_parts


class ProseSequenceTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.RotationConditionTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)

    def fixture(self):
        raw='1.点按普攻*3，长按普攻，点按e技能，长按至特殊动画，大招+声骸，切人。变奏入场可省略aaaz，ezrq直接启动。'
        block={'phase':'原文流程','prose':True,'lines':[raw],
               'sequence_anchors':['点按普攻*3','点按e技能','大招+声骸','切人']}
        spec={'id':'personal','format':'source-blocks','mode':'原文手法','chain_scope':'原文未标明链数','coverage_scope':'local_moves',
              'context':[],'phases':['原文流程'],'blocks':[block]}
        return raw,spec

    def test_full_paragraph_retains_conditional_omission_and_opaque_notation(self):
        raw,spec=self.fixture();page=self.fx.install(raw,[spec])
        segment=page['guide_curation']['rotations'][0]['segments'][0]
        self.assertEqual(segment['raw'],raw);self.assertEqual(segment['steps'],[raw])
        result=self.fx.fx.search('完整手法')[0]
        self.assertIn(raw,result['matches'][0]['text'])
        self.assertEqual(result['guide_context']['rotation_segments'][0]['step_format'],'source_paragraph')
        self.assertIn('省略aaaz',result['matches'][0]['text'])

    def test_missing_reordered_overlapping_duplicate_or_invented_anchor_is_rejected(self):
        raw,spec=self.fixture()
        for kind in ('short','missing','order','overlap','duplicate','title','not_prose'):
            bad=deepcopy(spec);block=bad['blocks'][0]
            if kind=='short':block['sequence_anchors']=block['sequence_anchors'][:2]
            elif kind=='missing':block['sequence_anchors'][1]='原文没有的步骤'
            elif kind=='order':block['sequence_anchors'].reverse()
            elif kind=='overlap':block['sequence_anchors']=['点按普攻*3','普攻*3','点按e技能']
            elif kind=='duplicate':block['sequence_anchors'][1]=block['sequence_anchors'][0]
            elif kind=='title':block['phase']='自创循环轴';bad['phases']=['自创循环轴']
            else:block['prose']=False
            with self.assertRaises(WikiError,msg=kind):rotation_parts(raw,bad)

    def test_absent_anchors_do_not_relax_old_unlabelled_prose_rule(self):
        raw,spec=self.fixture();del spec['blocks'][0]['sequence_anchors']
        with self.assertRaises(WikiError):rotation_parts(raw,spec)
        _,tiny=self.fixture();tiny['blocks'][0].update(lines=['AER'],sequence_anchors=['A','E','R'])
        with self.assertRaises(WikiError):rotation_parts('AER',tiny)

    def test_plus_symbols_and_resource_conditions_are_not_split_or_normalized(self):
        raw='变奏入场（20核心）＋2次空中攻击（50核心）＋大招（回地面）＋地面e技能＋2次空中攻击（50核心）＋强化e技能＋延奏走人'
        _,spec=self.fixture();spec['blocks'][0].update(lines=[raw],sequence_anchors=['变奏入场（20核心）','大招（回地面）','地面e技能','强化e技能','延奏走人'])
        self.assertEqual(rotation_parts(raw,spec)[0]['steps'],[raw])
        self.fx.install(raw,[spec]);self.assertIn(raw,self.fx.fx.search('完整手法')[0]['matches'][0]['text'])

    def test_local_paragraph_cannot_be_promoted_to_full_cycle_or_zero_chain(self):
        raw,spec=self.fixture();self.fx.install(raw,[spec])
        for query in ('完整循环轴','零链 手法'):
            for page in self.fx.fx.search(query):self.assertFalse(page.get('guide_context',{}).get('rotation_segments'))

    def test_budget_keeps_or_drops_whole_paragraph_including_omission(self):
        raw,spec=self.fixture();self.fx.install(raw,[spec])
        for budget in (1000,1800,3000,5000):
            pages=self.fx.fx.store.search('角色甲 手法',game_version='documents-v1',reviewed_only=True,max_chars=budget,
                document_filters={'category':'guide','entity':'character:1','query_entity_name':'角色甲','as_of':'2026-10-24T00:00:00+00:00'})
            for page in pages:
                for segment in page['guide_context']['rotation_segments']:
                    self.assertIn(raw,next(m['text'] for m in page['matches'] if m['claim_id']==segment['claim_id']))

    def test_unison_entry_and_later_swap_are_not_an_extra_initial_swap(self):
        _,spec=self.fixture()
        raw='首先队友获得同奏后，我们切心月狐同奏入场，开一段大，切队友下场。然后队友再切心月狐变奏入场后，AAAA再收尾。'
        spec['blocks'][0].update(lines=[raw],sequence_anchors=['首先队友获得同奏后，我们切心月狐同奏入场','切队友下场','队友再切心月狐变奏入场后'])
        self.fx.install(raw,[spec]);result=self.fx.fx.search('完整手法')[0]
        self.assertIn(raw,result['matches'][0]['text'])
        self.assertIn('第一次同奏入场前不能凭空再加一次',result['guide_context']['note'])


if __name__=='__main__':unittest.main()
