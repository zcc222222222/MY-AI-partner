"""Source-local annotations, exact rotation preservation and reviewed serving gates."""
from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
import test_dated_documents as fixtures
from wiki_test_helpers import publish_reviewed
from llmwiki import WikiError
from llmwiki.collector import import_document
from llmwiki.guide_curation import build_page, fingerprint, response_context, rotation_parts, rotation_scope_matches


class CurationTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.DocumentTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.store=self.fx.store;self.kb=Path(self.fx.tmp.name)/'kb.db'
        with closing(sqlite3.connect(self.kb)) as c,c:
            c.executescript("CREATE TABLE characters(id,name); INSERT INTO characters VALUES(1,'角色甲'),(2,'角色乙'); CREATE TABLE aliases(entity_type,entity_id,alias);")
        self.raw='角色甲与角色乙配队，甲与小甲指角色甲；乙指角色乙，另有未核实的丙。\n★【震谐】体系(默认为0链)轮椅轴\n【甲乙】轮椅轴：甲A3→切角色乙ER→角色甲A4(启动轴)；角色甲A4→延奏角色乙(循环轴)；'
        self.doc={'schema':'dated-source-v1','kind':'author_guide','title':'角色甲作者攻略','author':'测试作者',
                  'source_date':'2026-07-09','date_basis':'platform_modifiedAt','game_version':None,'document_version':'one',
                  'content':{'baseTexts':[{'language':'zh-Hans','introductionDetail':self.raw}]}}
        import_document(self.store,'g',self.doc,origin='https://mcguide.kurogames.com/',entities=['character:1'])
        publish_reviewed(self.store,self.fx.compile('g')['draft_id'])
        self.source=self.store.get_source('g',game_version='documents-v1')
        self.page=self.store.get_page('page/g0',game_version='documents-v1')['page']
        self.spec={'id':'shock','mode':'震谐','chain_scope':'默认为0链','heading':'★【震谐】体系(默认为0链)轮椅轴','prefix':'【甲乙】轮椅轴：','phases':['启动轴','循环轴']}
        self.manifest={'schema':'guide-curation-v1','source_id':'g','source_revision':1,'source_sha256':fingerprint(self.source),
                       'source_path':['content','baseTexts',0,'introductionDetail'],'terms':[{'aliases':['甲','小甲'],'name':'角色甲','target_ids':['1'],'support':['甲与小甲指角色甲']}],
                       'unresolved_terms':['丙'],'rotations':[self.spec]}

    def build(self):return build_page(self.source,self.page,self.manifest,self.kb)
    def search(self,query='震谐 完整输出轴'):
        return self.fx.search(query,category='guide',entity='character:1',query_entity_name='角色甲')

    def test_exact_source_phases_are_preserved_and_no_global_alias_is_written(self):
        page=self.build();data=page['guide_curation']
        self.assertEqual(data['terms'][0]['target_ids'],['1'])
        startup,cycle=data['rotations'][0]['segments']
        self.assertEqual(startup['raw'],'甲A3→切角色乙ER→角色甲A4')
        self.assertEqual(cycle['raw'],'角色甲A4→延奏角色乙')
        self.assertEqual('→'.join(startup['steps']),startup['raw'])
        with closing(sqlite3.connect(self.kb)) as c:self.assertEqual(c.execute('SELECT count(*) FROM aliases').fetchone()[0],0)

    def test_curation_is_not_served_before_approval_then_carries_local_context(self):
        draft=self.store.stage_page(self.build());self.assertTrue(draft['valid'],draft)
        self.assertFalse(any('guide_context' in p for p in self.search()))
        publish_reviewed(self.store,draft['draft_id'])
        result=self.search()
        self.assertTrue(result)
        context=result[0]['guide_context']
        self.assertEqual(context['scope'],'this_source_revision_only')
        self.assertEqual({s['phase'] for s in context['rotation_segments']},{'启动轴','循环轴'})
        self.assertNotIn('guide_context',self.page)

    def test_single_phase_query_does_not_claim_to_return_the_whole_rotation(self):
        publish_reviewed(self.store,self.store.stage_page(self.build())['draft_id'])
        result=self.search('震谐 循环轴')
        segments=result[0]['guide_context']['rotation_segments']
        self.assertEqual([s['phase'] for s in segments],['循环轴'])

    def test_pending_alias_remains_pending_and_other_documents_do_not_inherit(self):
        page=self.build()
        context=response_context(page,[{'claim_id':'x','text':'丙'}],'缩写丙')
        self.assertEqual(context['unresolved_terms'],['丙'])
        self.assertFalse(context['terms'])
        self.assertEqual(response_context(self.page,[{'claim_id':'x','text':'小甲'}]),{})

    def test_modified_steps_or_mode_or_term_target_name_fail_lint(self):
        for kind in ('steps','mode','name'):
            page=self.build()
            if kind=='steps':page['guide_curation']['rotations'][0]['segments'][0]['steps'].reverse()
            elif kind=='mode':page['guide_curation']['rotations'][0]['spec']['mode']='聚爆'
            else:page['guide_curation']['terms'][0]['name']='角色乙'
            report=self.store.stage_page(page)
            self.assertFalse(report['valid'],kind)
            self.assertTrue(any('invalid_document_scope' in e for e in report['errors']))

    def test_source_revision_change_invalidates_page_and_prevents_old_manifest_reuse(self):
        publish_reviewed(self.store,self.store.stage_page(self.build())['draft_id'])
        self.doc['document_version']='two'
        import_document(self.store,'g',self.doc,origin='https://mcguide.kurogames.com/',entities=['character:1'])
        self.assertFalse(self.search())
        newer=self.store.get_source('g',game_version='documents-v1')
        with self.assertRaises(WikiError):build_page(newer,self.page,self.manifest,self.kb)

    def test_wrong_id_or_missing_support_is_rejected(self):
        self.manifest['terms'][0]['target_ids']=['2']
        with self.assertRaises(WikiError):self.build()
        self.manifest['terms'][0]['target_ids']=['1']
        self.manifest['terms'][0]['support']=['未出现的定义']
        with self.assertRaises(WikiError):self.build()

    def test_rejected_latest_review_prevents_serving(self):
        draft=self.store.stage_page(self.build());publish_reviewed(self.store,draft['draft_id'])
        self.store.review(draft['draft_id'],decision='rejected',reviewer='fixture',reviewer_type='agent',note='annotation withdrawn')
        self.assertFalse(self.search())

    def test_duplicate_or_incomplete_axis_is_not_guessed_and_arrow_gap_is_preserved(self):
        with self.assertRaises(WikiError):rotation_parts(self.raw.replace('(循环轴)',''),self.spec)
        with self.assertRaises(WikiError):rotation_parts(self.raw+'\n【甲乙】轮椅轴：重复',self.spec)
        parts=rotation_parts(self.raw.replace('甲A3→','甲A3→→'),self.spec)
        self.assertTrue(parts[0]['notation_gap'])
        self.assertIn('',parts[0]['steps'])

    def test_reapply_does_not_duplicate_prior_annotations(self):
        first=self.build()
        second=build_page(self.source,first,self.manifest,self.kb)
        self.assertEqual(len(first['claims']),len(second['claims']))
        self.assertEqual(first['body'],second['body'])

    def test_other_chain_or_advanced_axis_does_not_reuse_zero_chain_full_segments(self):
        publish_reviewed(self.store,self.store.stage_page(self.build())['draft_id'])
        for query in ('震谐 3链 完整输出轴','震谐 进阶 输出轴'):
            result=self.search(query)
            for page in result:
                self.assertFalse(page['guide_context']['rotation_segments'])
                self.assertIn('uncovered_rotation_request',page['guide_context'])

    def line_fixture(self):
        raw='（e：共鸣技能；r：共鸣解放）\n注：需要打满协奏能量\n配队手法：角色甲e→角色乙r→循环轴\n循环轴：角色乙退场→角色甲e→打满协奏能量'
        spec={'id':'team','format':'exact-lines','mode':'原文配队','chain_scope':'原文未标明链数',
              'context':raw.splitlines()[:2],'phases':['配队手法','循环轴'],
              'lines':[{'phase':p,'line':line} for p,line in zip(['配队手法','循环轴'],raw.splitlines()[2:])]}
        return raw,spec

    def test_exact_lines_preserve_conditions_and_reject_changed_reordered_or_duplicate_steps(self):
        raw,spec=self.line_fixture()
        self.assertEqual([p['raw'] for p in rotation_parts(raw,spec)],raw.splitlines()[2:])
        for changed in (raw.replace('角色乙r','角色乙e'),raw+'\n'+spec['lines'][0]['line'],
                        '\n'.join(raw.splitlines()[:2]+raw.splitlines()[2:][::-1]),raw.replace('注：需要打满协奏能量','')):
            with self.assertRaises(WikiError):rotation_parts(changed,spec)
        spec['chain_scope']='0链'
        with self.assertRaises(WikiError):rotation_parts(raw,spec)

    def test_unspecified_chain_is_not_zero_and_explicit_zero_matches_chinese_zero(self):
        _,spec=self.line_fixture()
        for query in ('零链 输出轴','0链 循环轴','6链 手法','高链 输出轴','进阶 输出轴','震谐 输出轴'):
            self.assertFalse(rotation_scope_matches(spec,query),query)
        self.assertTrue(rotation_scope_matches(spec,'完整启动轴与循环轴'))
        self.assertTrue(rotation_scope_matches(self.spec,'零链 循环轴'))
        self.assertTrue(rotation_scope_matches(self.spec,'0链 循环轴'))
        self.assertFalse(rotation_scope_matches(self.spec,'聚爆 零链 循环轴'))

    def test_exact_lines_serving_preserves_context_and_does_not_label_unknown_chain(self):
        raw,spec=self.line_fixture()
        self.doc['document_version']='lines';self.doc['content']['baseTexts'][0]['introductionDetail']=raw
        import_document(self.store,'g',self.doc,origin='https://mcguide.kurogames.com/',entities=['character:1'])
        publish_reviewed(self.store,self.fx.compile('g')['draft_id'])
        self.source=self.store.get_source('g',game_version='documents-v1')
        self.page=self.store.get_page('page/g0',game_version='documents-v1')['page']
        self.manifest.update(source_revision=2,source_sha256=fingerprint(self.source),rotations=[spec],terms=[])
        publish_reviewed(self.store,self.store.stage_page(self.build())['draft_id'])
        result=self.search('完整启动轴与循环轴')
        segs=result[0]['guide_context']['rotation_segments']
        self.assertEqual([s['phase'] for s in segs],['配队手法','循环轴'])
        for m in result[0]['matches']:
            if m['claim_id'].startswith('curation-rotation-'):self.assertIn('注：需要打满协奏能量',m['text'])
        cycle=self.search('循环轴')[0]['guide_context']['rotation_segments']
        self.assertEqual([s['phase'] for s in cycle],['循环轴'])
        self.assertNotIn('配队手法：',' '.join(m['text'] for m in self.search('循环轴')[0]['matches']))
        opening=self.search('完整开场手法 循环轴')[0]['guide_context']['rotation_segments']
        self.assertEqual([s['phase'] for s in opening],['配队手法','循环轴'])
        pair=self.search('完整输出轴 配队手法 循环轴')[0]['guide_context']['rotation_segments']
        self.assertEqual([s['phase'] for s in pair],['配队手法','循环轴'])
        opening_only=self.search('启动轴')[0]['guide_context']['rotation_segments']
        self.assertEqual([s['phase'] for s in opening_only],['配队手法'])
        for query in ('零链 完整输出轴','0链 完整输出轴','3链 循环轴','0链 输出轴 手法 配队攻略','震谐 配队攻略 手法'):
            for p in self.search(query):
                self.assertFalse(p['guide_context']['rotation_segments'])
                self.assertIn('uncovered_rotation_request',p['guide_context'])
                self.assertFalse(any('→' in m['text'] for m in p['matches']))

    def block_fixture(self):
        raw='操作提示：满能量后退场\n基础手法1：角色甲A→角色乙E\n详细过程：角色甲普攻→角色乙共鸣技能后退场\n基础手法2：角色甲Z→角色乙R\n详细过程：角色甲重击→角色乙共鸣解放后退场'
        lines=raw.splitlines()
        spec={'id':'variants','format':'source-blocks','mode':'原文手法','chain_scope':'原文未标明链数',
              'context':lines[:1],'phases':['基础手法1','基础手法2'],
              'blocks':[{'phase':'基础手法1','lines':lines[1:3]},{'phase':'基础手法2','lines':lines[3:5]}]}
        return raw,spec

    def install_block_fixture(self,raw,spec):
        self.doc['document_version']='blocks';self.doc['content']['baseTexts'][0]['introductionDetail']=raw
        import_document(self.store,'g',self.doc,origin='https://mcguide.kurogames.com/',entities=['character:1'])
        publish_reviewed(self.store,self.fx.compile('g')['draft_id'])
        self.source=self.store.get_source('g',game_version='documents-v1')
        self.page=self.store.get_page('page/g0',game_version='documents-v1')['page']
        self.manifest.update(source_revision=2,source_sha256=fingerprint(self.source),rotations=[spec],terms=[])
        built=self.build();publish_reviewed(self.store,self.store.stage_page(built)['draft_id'])
        return built

    def test_source_blocks_keep_both_representations_and_reject_incomplete_reordered_or_overlapping_blocks(self):
        raw,spec=self.block_fixture();parts=rotation_parts(raw,spec)
        self.assertEqual(parts[0]['raw'],'\n'.join(raw.splitlines()[1:3]))
        for changed in (raw.replace('角色乙共鸣技能后退场','角色乙共鸣技能'),raw+'\n'+'\n'.join(raw.splitlines()[1:3])):
            with self.assertRaises(WikiError):rotation_parts(changed,spec)
        for kind in ('order','overlap','phase','selector','chain'):
            bad=deepcopy(spec)
            if kind=='order':bad['blocks'].reverse();bad['phases'].reverse()
            elif kind=='overlap':bad['blocks'][1]['lines']=bad['blocks'][0]['lines']
            elif kind=='phase':bad['blocks'][0]['phase']='循环轴';bad['phases'][0]='循环轴'
            elif kind=='selector':bad['blocks'][0]['selectors']=['角色丙']
            elif kind=='chain':bad['chain_scope']='0链'
            with self.assertRaises(WikiError,msg=kind):rotation_parts(raw,bad)

    def test_source_variant_query_returns_only_its_full_short_and_detailed_representations(self):
        raw,spec=self.block_fixture();self.install_block_fixture(raw,spec)
        result=self.search('基础手法1 完整流程')[0]
        self.assertEqual([s['phase'] for s in result['guide_context']['rotation_segments']],['基础手法1'])
        text=' '.join(m['text'] for m in result['matches'])
        self.assertIn('详细过程：角色甲普攻→角色乙共鸣技能后退场',text)
        self.assertNotIn('基础手法2',text)
        self.assertIn('操作提示：满能量后退场',text)

    def test_unlabelled_flow_is_not_promoted_to_startup_and_cycle_and_keys_stay_undefined(self):
        raw='角色甲AE→角色乙Z→角色甲退场'
        spec={'id':'unlabelled','format':'source-blocks','mode':'原文手法','chain_scope':'原文未标明链数',
              'context':[],'phases':['原文流程'],'blocks':[{'phase':'原文流程','lines':[raw]}]}
        built=self.install_block_fixture(raw,spec)
        matches=[{'claim_id':s['claim_id'],'text':s['claim_text']} for s in built['guide_curation']['rotations'][0]['segments']]
        self.assertEqual(response_context(built,matches,'完整手法')['rotation_segments'][0]['phase'],'原文流程')
        self.assertIn('是应用分类标签，不是作者标注的标题',response_context(built,matches,'完整手法')['note'])
        context=response_context(built,[],'启动轴 循环轴')
        self.assertIn('uncovered_phase_request',context)
        for result in self.search('角色甲 启动轴 循环轴'):
            self.assertFalse(result['guide_context']['rotation_segments'])
            self.assertIn('uncovered_phase_request',result['guide_context'])
            self.assertFalse(any('→' in m['text'] for m in result['matches']))

    def test_partial_phase_coverage_and_team_variant_selectors_are_explicit(self):
        raw='队伍甲：角色甲A→角色乙E\n队伍乙：角色甲A→角色乙R'
        spec={'id':'teams','format':'source-blocks','mode':'原文手法','chain_scope':'原文未标明链数','context':[],
              'phases':['队伍甲','队伍乙'],'blocks':[{'phase':p,'selectors':[p],'lines':[l]} for p,l in zip(['队伍甲','队伍乙'],raw.splitlines())]}
        self.install_block_fixture(raw,spec)
        result=self.search('队伍乙 完整手法')[0]
        self.assertEqual([s['phase'] for s in result['guide_context']['rotation_segments']],['队伍乙'])
        # An opening-only source still cannot claim to contain a cycle.
        page=self.build();flow=page['guide_curation']['rotations'][0]
        flow['segments']=[{**flow['segments'][0],'phase':'配队手法'}]
        context=response_context(page,[],'启动轴 循环轴')
        self.assertIn('循环轴',context['uncovered_phase_request'])

    def test_verified_teammate_chain_is_local_and_never_confirms_the_owner_chain(self):
        raw='角色乙队伍（1链角色乙）：角色甲A→角色乙R\n备用队伍：角色甲A→角色丙E'
        spec={'id':'conditions','format':'source-blocks','mode':'原文手法','chain_scope':'原文未标明链数','context':[],
              'phases':['角色乙队伍','备用队伍'],
              'blocks':[{'phase':p,'selectors':[cue],'lines':[line]} for p,cue,line in zip(['角色乙队伍','备用队伍'],['角色乙','备用'],raw.splitlines())],
              'teammate_chain_conditions':[{'text':'1链角色乙','chain':1,'name':'角色乙','target_ids':['2']}]}
        self.doc['role_id']='1';built=self.install_block_fixture(raw,spec)
        for query in ('1链角色乙 手法','一链角色乙 手法','角色乙1链 手法'):
            result=self.search(query)[0]
            self.assertEqual([s['phase'] for s in result['guide_context']['rotation_segments']],['角色乙队伍'])
            self.assertEqual(result['guide_context']['teammate_chain_conditions'][0]['target_ids'],['2'])
            self.assertEqual(result['guide_context']['rotation_segments'][0]['chain_scope'],'原文未标明链数')
        self.assertNotIn('teammate_chain_conditions',self.search('备用队伍 手法')[0]['guide_context'])
        for query in ('角色甲1链 1链角色乙 手法','角色甲零链 角色乙1链 手法','11链角色乙 手法'):
            self.assertFalse(rotation_scope_matches(spec,query),query)
            for result in self.search(query):self.assertFalse(result['guide_context']['rotation_segments'])
        bad=deepcopy(built);bad['guide_curation']['rotations'][0]['spec']['teammate_chain_conditions'][0]['target_ids']=['1']
        lint=self.store.stage_page(bad);self.assertFalse(lint['valid'])
        self.assertTrue(any('不能把攻略主体的链数' in e for e in lint['errors']))


if __name__=='__main__':unittest.main()
