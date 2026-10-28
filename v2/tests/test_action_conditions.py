"""Inline annotations are not chain applicability; shared source is not shared startup."""
from copy import deepcopy
import unittest
import test_rotation_conditions as fixtures
from llmwiki import WikiError
from llmwiki.guide_curation import rotation_parts, rotation_scope_matches


class ActionConditionTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.RotationConditionTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)

    def action_fixture(self):
        raw='角色甲E(4链) 切角色乙A→角色甲R'
        note={'action':'角色甲E','quote':'角色甲E(4链)','name':'角色甲','target_ids':['1'],'chain':4}
        spec={'id':'inline','format':'source-blocks','mode':'原文手法','chain_scope':'原文未标明链数',
              'context':[],'phases':['原文流程'],'blocks':[{'phase':'原文流程','lines':[raw],'action_chain_annotations':[note]}]}
        return raw,spec

    def test_inline_marker_is_preserved_without_becoming_owner_scope(self):
        raw,spec=self.action_fixture();page=self.fx.install(raw,[spec])
        seg=page['guide_curation']['rotations'][0]['segments'][0]
        note=seg['action_chain_annotations'][0]
        self.assertEqual(raw[note['start']:note['end']],note['quote'])
        self.assertEqual(note['scope'],'source_action_only')
        self.assertNotIn('owner_chain_scope',seg)
        result=self.fx.fx.search('完整手法')[0]
        self.assertIn(raw,result['matches'][0]['text'])
        self.assertEqual(result['guide_context']['action_chain_annotations'][0]['chain'],4)

    def test_zero_four_and_full_chain_remain_unknown_but_explain_exact_annotation(self):
        raw,spec=self.action_fixture();self.fx.install(raw,[spec])
        for query in ('零链 完整手法','4链 完整手法','满链 完整手法','0—4链 完整手法'):
            self.assertFalse(rotation_scope_matches(spec,query))
            pages=self.fx.fx.search(query);self.assertTrue(pages,query)
            ctx=pages[0]['guide_context']
            self.assertFalse(ctx['rotation_segments'])
            self.assertIn('uncovered_rotation_request',ctx)
            self.assertEqual(ctx['action_chain_annotations'][0]['quote'],'角色甲E(4链)')
            self.assertNotIn('角色甲R','\n'.join(m['text'] for m in pages[0]['matches']))

    def test_modified_action_quote_chain_duplicate_or_identity_is_rejected(self):
        raw,spec=self.action_fixture()
        for key,value in (('quote','角色甲E(3链)'),('action','角色甲R'),('chain',True),('chain',3),('name','角色乙')):
            bad=deepcopy(spec);bad['blocks'][0]['action_chain_annotations'][0][key]=value
            with self.assertRaises(WikiError,msg=key):rotation_parts(raw,bad)
        bad=deepcopy(spec);bad['blocks'][0]['action_chain_annotations']*=2
        with self.assertRaises(WikiError):rotation_parts(raw,bad)
        self.fx.install(raw,[spec]);bad=deepcopy(spec)
        bad['blocks'][0]['action_chain_annotations'][0]['target_ids']=['2']
        self.fx.fx.manifest['rotations']=[bad]
        with self.assertRaises(WikiError):self.fx.fx.build()

    def test_quoting_marker_does_not_create_owner_chain_request(self):
        raw,spec=self.action_fixture();self.fx.install(raw,[spec])
        query='完整手法 角色甲E(4链)'
        self.assertTrue(rotation_scope_matches(spec,query))
        self.assertTrue(self.fx.fx.search(query)[0]['guide_context']['rotation_segments'])
        self.assertTrue(rotation_scope_matches(spec,'完整手法 角色甲E 4链'))
        self.assertFalse(rotation_scope_matches(spec,'角色甲零链 完整手法 角色甲E 4链'))
        for prefix in ('角色甲零链','角色甲4链','满链'):
            self.assertFalse(rotation_scope_matches(spec,prefix+' '+query))

    def test_tampered_derived_span_is_rejected_by_publish_lint(self):
        raw,spec=self.action_fixture();page=self.fx.install(raw,[spec])
        page['guide_curation']['rotations'][0]['segments'][0]['action_chain_annotations'][0]['start']=5
        self.assertFalse(self.fx.fx.store.stage_page(page)['valid'])

    def team_fixture(self):
        context=['2.进阶连招（5R轴）','★：表示有变奏buff，额外回复30点回路。']
        starts=['(清/达双人启动)-角色甲E→A4','(清/达/莫三人启动)-角色甲E→A1A2*2']
        cycle='②循环招式：角色甲E★→A4→退场'
        specs=[]
        for n,label in enumerate(('清/达双人启动','清/达/莫三人启动')):
            specs.append({'id':str(n),'format':'source-blocks','mode':'原文手法','chain_scope':'原文未标明链数',
                          'context':context,'advanced_scope':context[0],
                          'team_variant':{'label':label,'quote':'('+label+')','selectors':['双人' if n==0 else '三人']},
                          'phases':[label,'②循环招式'],
                          'blocks':[{'phase':label,'lines':[starts[n]]},{'phase':'②循环招式','lines':[cycle]}]})
        return '\n'.join(context[:1]+starts+[cycle]+context[1:]),specs

    def test_variants_keep_distinct_startups_and_same_cycle_with_star_condition(self):
        raw,specs=self.team_fixture();self.fx.install(raw,specs)
        for cue,expected,other in [('双人','A4','A1A2*2'),('三人','A1A2*2','角色甲E→A4')]:
            page=self.fx.fx.search(cue+' 进阶 启动轴 循环轴')[0]
            text='\n'.join(m['text'] for m in page['matches'])
            self.assertIn(expected,text);self.assertNotIn(other,text)
            self.assertIn('②循环招式：角色甲E★→A4→退场',text)
            self.assertIn('额外回复30点回路',text)
            self.assertEqual(len(page['guide_context']['rotation_segments']),2)
            self.assertTrue(all(cue in s['team_variant'] for s in page['guide_context']['rotation_segments']))
        cycle=self.fx.fx.search('双人 循环轴')[0]
        self.assertEqual([s['phase'] for s in cycle['guide_context']['rotation_segments']],['②循环招式'])
        self.assertNotIn('A1A2*2','\n'.join(m['text'] for m in cycle['matches']))

    def test_team_label_and_advanced_scope_require_own_original_context(self):
        raw,specs=self.team_fixture()
        bad=deepcopy(specs[0]);bad['team_variant']=specs[1]['team_variant']
        with self.assertRaises(WikiError):rotation_parts(raw,bad)
        bad=deepcopy(specs[0]);bad['advanced_scope']='6R进阶轴'
        with self.assertRaises(WikiError):rotation_parts(raw,bad)

    def test_longer_team_label_does_not_select_its_shorter_prefix(self):
        from llmwiki.guide_curation import matching_flows
        raw,specs=self.team_fixture()
        specs[0]['team_variant']['selectors'].append('清/达')
        specs[1]['team_variant']['selectors'].append('清/达/莫')
        self.fx.install(raw,specs)
        page=self.fx.fx.search('清/达/莫 进阶启动轴 循环轴')[0]
        self.assertEqual({s['team_variant'] for s in page['guide_context']['rotation_segments']},{'清/达/莫三人启动'})
        flows=matching_flows({'rotations':[{'spec':s} for s in specs]},'比较清/达双人和清/达/莫三人')
        self.assertEqual(len(flows),2)
        annotated=self.fx.fx.search('清/达/莫 三人 进阶启动招式 循环招式 缩写 ★')[0]
        self.assertEqual(len(annotated['guide_context']['rotation_segments']),2)


if __name__=='__main__':unittest.main()
