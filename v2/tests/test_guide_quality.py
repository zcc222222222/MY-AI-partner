"""Quality holds suppress advice without deleting history or bypassing source review."""
from copy import deepcopy
import unittest
from unittest.mock import patch
import test_rotation_conditions as fixtures
from llmwiki import WikiError
from llmwiki.collector import import_document
from llmwiki.guide_quality import build_quality_page, fingerprint
from wiki_test_helpers import publish_reviewed


class GuideQualityTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.RotationConditionTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        raw='输出流程：角色甲A→E(可疑技能)→R'
        spec={'id':'one','format':'source-blocks','mode':'原文手法','chain_scope':'原文未标明链数',
              'context':[],'phases':['输出流程'],'blocks':[{'phase':'输出流程','lines':[raw]}]}
        self.page=self.fx.install(raw,[spec]);self.core=self.fx.fx
        self.manifest={'schema':'guide-quality-v1','source_id':'g','source_revision':2,'source_sha256':fingerprint(self.core.source),
                       'status':'pending_verification','issues':[{'code':'skill_attribution','source_path':['content','baseTexts',0,'introductionDetail'],'quote':'E(可疑技能)'}]}

    def hold(self):
        page=build_quality_page(self.core.source,self.page,self.manifest)
        draft=self.core.store.stage_page(page);self.assertTrue(draft['valid'],draft)
        publish_reviewed(self.core.store,draft['draft_id']);return page

    def test_published_hold_returns_only_metadata_for_all_topics(self):
        held=self.hold();self.assertEqual(held['claims'],self.page['claims'])
        for query in ('完整手法','技能 可疑技能','配队 武器 声骸','零链 输出轴','缩写 原图'):
            pages=self.core.search(query);self.assertTrue(pages,query)
            for page in pages:
                self.assertEqual(page['quality_review']['status'],'pending_verification')
                self.assertNotIn('guide_context',page)
                self.assertTrue(all(m['claim_id']=='claim-001' for m in page['matches']))
                self.assertNotIn('可疑技能',page['snippet'])
        old=self.core.store.get_page('page/g0',game_version='documents-v1',revision=2)
        self.assertIn('可疑技能',old['page']['body'])

    def test_hold_not_served_before_approval_and_rejected_review_is_not_served(self):
        held=build_quality_page(self.core.source,self.page,self.manifest);draft=self.core.store.stage_page(held)
        self.assertTrue(self.core.search('完整手法')[0]['guide_context']['rotation_segments'])
        publish_reviewed(self.core.store,draft['draft_id'])
        self.core.store.review(draft['draft_id'],decision='rejected',reviewer='fixture',reviewer_type='agent',note='withdraw review')
        self.assertFalse(self.core.search('完整手法'))

    def test_bad_pins_status_and_quotes_fail_lint(self):
        for key,value in (('source_revision',1),('source_sha256','invented'),('status','verified')):
            bad=deepcopy(self.manifest);bad[key]=value
            with self.assertRaises(WikiError):build_quality_page(self.core.source,self.page,bad)
        bad=deepcopy(self.manifest);bad['issues'][0]['quote']='来源没有这句话'
        with self.assertRaises(WikiError):build_quality_page(self.core.source,self.page,bad)

    def test_recompiling_or_same_revision_resolution_cannot_clear_hold(self):
        self.hold()
        self.assertFalse(self.core.store.stage_page(self.page)['valid'])
        bad=deepcopy(self.manifest);bad.update(status='resolved',resolution='already fixed',supersedes_source_revision=2)
        with self.assertRaises(WikiError):build_quality_page(self.core.source,self.page,bad)

    def test_updated_source_requires_fresh_review_before_resolution(self):
        self.hold();self.core.doc['document_version']='quality-fixed'
        self.core.doc['content']['baseTexts'][0]['introductionDetail']='输出流程：角色甲A→E(新技能)→R'
        import_document(self.core.store,'g',self.core.doc,origin='https://mcguide.kurogames.com/',entities=['character:1'])
        self.assertFalse(self.core.search('完整手法'))
        source=self.core.store.get_source('g',game_version='documents-v1')
        # Simulate normal recompilation, with unchanged supported evidence shape.
        # This fixture normally insists compilation succeeds; failure is the
        # expected production gate for a missing sticky quality annotation.
        with patch.object(self.core.fx,'assertTrue'):draft=self.core.fx.compile('g')
        self.assertFalse(draft['valid'])
        with self.core.store.connection() as c:
            import json
            page=json.loads(c.execute('SELECT payload FROM drafts WHERE draft_id=?',(draft['draft_id'],)).fetchone()[0])
        manifest=deepcopy(self.manifest);manifest.update(source_revision=3,source_sha256=fingerprint(source),status='resolved',supersedes_source_revision=2,resolution='新来源修订已核对技能名称，仍按作者观点使用')
        manifest['issues'][0]['quote']='E(新技能)'
        fixed=build_quality_page(source,page,manifest);staged=self.core.store.stage_page(fixed)
        self.assertTrue(staged['valid'],staged)
        publish_reviewed(self.core.store,staged['draft_id'])
        result=self.core.search('输出流程')[0]
        self.assertEqual(result['quality_review']['status'],'resolved')
        self.assertIn('新技能',result['snippet'])

    def test_named_prose_heading_accepts_full_rows_not_fabricated_heading(self):
        from llmwiki.guide_curation import rotation_parts
        raw='队伍流程\n角色甲E 切角色乙\n角色乙R 切角色甲，协奏满后退场'
        spec={'id':'prose','format':'source-blocks','mode':'原文手法','chain_scope':'原文未标明链数','context':[],
              'phases':['队伍流程'],'blocks':[{'phase':'队伍流程','prose':True,'lines':raw.splitlines()}]}
        parts=rotation_parts(raw,spec);self.assertEqual(parts[0]['raw'],raw)
        self.assertEqual(parts[0]['step_format'],'source_prose')
        spec['phases']=['自创循环轴'];spec['blocks'][0]['phase']='自创循环轴'
        with self.assertRaises(WikiError):rotation_parts(raw,spec)


if __name__=='__main__':unittest.main()
