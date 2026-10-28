"""Additional text fields keep exact evidence IDs and explicit coverage limits."""
from copy import deepcopy
import unittest
import test_rotation_conditions as fixtures
from wiki_test_helpers import publish_reviewed
from llmwiki.collector import import_document
from llmwiki.documents import evidence,validate_page
from llmwiki.guide_curation import build_page,fingerprint
from llmwiki import WikiError


class MultifieldCurationTests(unittest.TestCase):
    def setUp(self):
        self.wrapper=fixtures.RotationConditionTests();self.wrapper.setUp();self.addCleanup(self.wrapper.doCleanups)
        self.fx=self.wrapper.fx

    def fixture(self):
        fx=self.fx;doc=deepcopy(fx.doc);doc['document_version']='multi'
        doc['content']['baseTexts'][0].update(introductionDetail='正文只介绍角色定位。',introductionSynopsis='摘要手法：变奏→E→R→退场',skillDisplayDescription='基础连招：普攻→跳跃→技能→延奏')
        import_document(fx.store,'g',doc,origin='https://mcguide.kurogames.com/',entities=['character:1'])
        publish_reviewed(fx.store,fx.fx.compile('g')['draft_id'])
        source=fx.store.get_source('g',game_version='documents-v1');page=fx.store.get_page('page/g0',game_version='documents-v1')['page']
        manifest=deepcopy(fx.manifest);manifest.update(source_revision=2,source_sha256=fingerprint(source),terms=[],unresolved_terms=[],rotations=[])
        for field,title in [('introductionSynopsis','摘要手法'),('skillDisplayDescription','基础连招')]:
            manifest['rotations'].append({'id':field,'format':'source-blocks','mode':'原文手法','chain_scope':'原文未标明链数','context':[],
                'source_path':['content','baseTexts',0,field],'phases':[title],'blocks':[{'phase':title,'lines':[doc['content']['baseTexts'][0][field]]}]})
        return source,page,manifest

    def test_append_preserves_existing_evidence_and_claims_and_binds_each_field(self):
        source,old,manifest=self.fixture();before=evidence(source,old)
        page=build_page(source,old,manifest,self.fx.kb);after=evidence(source,page)
        self.assertEqual(after[:len(before)],before)
        self.assertEqual(len(after),len(before)+1)
        self.assertTrue(all(c in page['claims'] for c in old['claims']))
        for f in page['guide_curation']['rotations']:
            claim=next(c for c in page['claims'] if c['claim_id']==f['segments'][0]['claim_id'])
            self.assertEqual(claim['citations'][0]['source_path'],f['spec']['source_path'])
        validate_page(source,page)
        draft=self.fx.store.stage_page(page);self.assertTrue(draft['valid'],draft);publish_reviewed(self.fx.store,draft['draft_id'])
        result=self.fx.search('完整手法')[0]
        self.assertEqual(len(result['guide_context']['rotation_segments']),2)
        self.assertIn('不同原文方案',result['guide_context']['note'])

    def test_unreviewed_panel_or_wrong_field_citation_rejected(self):
        source,old,manifest=self.fixture()
        bad=deepcopy(manifest);bad['rotations'][0]['source_path'][-1]='introductionName'
        with self.assertRaises(WikiError):build_page(source,old,bad,self.fx.kb)
        page=build_page(source,old,manifest,self.fx.kb)
        page['guide_curation']['extra_source_paths'][0][-1]='roleDescription'
        with self.assertRaises(WikiError):validate_page(source,page)
        page=build_page(source,old,manifest,self.fx.kb);claims=[c for c in page['claims'] if c['claim_id'].startswith('curation-rotation-')]
        claims[0]['citations']=deepcopy(claims[1]['citations']);claims[0]['evidence_ids']=claims[1]['evidence_ids']
        with self.assertRaises(WikiError):validate_page(source,page)

    def test_no_sequence_review_gates_rotation_only_not_build_advice(self):
        source,old,manifest=self.fixture();manifest['rotations']=[]
        manifest['coverage_review']={'status':'no_action_sequence','reviewed_paths':[manifest['source_path']],
            'note':'本次只核对定位字段，未有完整动作顺序，不代表全站没有。','blockers':[]}
        page=build_page(source,old,manifest,self.fx.kb);draft=self.fx.store.stage_page(page);self.assertTrue(draft['valid']);publish_reviewed(self.fx.store,draft['draft_id'])
        self.assertEqual(build_page(source,page,manifest,self.fx.kb)['body'],page['body'])
        result=self.fx.search('完整手法')[0]
        self.assertFalse(result['guide_context']['rotation_segments']);self.assertIn('未有完整动作顺序',result['guide_context']['uncovered_rotation_request'])
        self.assertNotIn('摘要手法',str(result['matches']))
        self.assertNotIn('变奏→E',str(result['matches']))
        self.assertTrue(self.fx.search('攻略'))
        for page in self.fx.search('手法 武器'):
            self.assertNotIn('正文只介绍角色定位',str(page['matches']))
            self.assertFalse(page['guide_context']['rotation_segments'])

    def test_coverage_review_cannot_invent_boundary_quotes(self):
        source,old,manifest=self.fixture();manifest['coverage_review']={'status':'ambiguous_chain_boundary','reviewed_paths':[manifest['source_path']],'note':'边界待核对','blockers':[]}
        with self.assertRaises(WikiError):build_page(source,old,manifest,self.fx.kb)


if __name__=='__main__':unittest.main()
