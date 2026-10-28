"""Gap checks preserve holds; historical evidence has independent scope and lifecycle."""
from copy import deepcopy
import unittest
import test_guide_curation as fixtures
from wiki_test_helpers import publish_reviewed
from llmwiki import WikiError
from llmwiki.collector import import_document
from llmwiki.guide_curation import fingerprint
from llmwiki.guide_curation import build_page
from llmwiki.guide_followup import build_followup_page
from llmwiki.guide_quality import build_quality_page


class GuideFollowupTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.CurationTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)

    def manifest(self):
        return {'schema':'guide-followup-v1','source_id':'g','source_revision':1,'source_sha256':fingerprint(self.fx.source),
                'status':'open','original_issue_resolved':False,'checked_at':'2026-10-24T12:00:00+08:00',
                'checks':[{'kind':'local_crosscheck','source_path':self.fx.manifest['source_path'],
                           'quote':'角色甲与角色乙配队','note':'原段仍须按来源条件解释，不解除原篇限制。'}], 'historical_pages':[]}

    def install(self,page):
        draft=self.fx.store.stage_page(page);self.assertTrue(draft['valid'],draft);publish_reviewed(self.fx.store,draft['draft_id'])

    def history(self):
        doc={'schema':'dated-source-v1','kind':'author_guide','title':'历史角色甲手法','author':'另一作者','source_date':'2026-01-01',
             'date_basis':'article_display_date','game_version':'2.2','document_version':'history1','source_role':'historical_supplement','supplement_for':'g',
             'content':{'baseTexts':[{'language':'zh-Hans','introductionDetail':'历史手法：角色甲E→R→退场。'}]}}
        url='https://www.taptap.cn/moment/719276523214865858'
        import_document(self.fx.store,'h',doc,origin=url,entities=['character:1'])
        publish_reviewed(self.fx.store,self.fx.fx.compile('h')['draft_id'])
        page=self.fx.store.get_page('page/h0',game_version='documents-v1');ref={'page_id':'page/h0','revision':page['revision'],
                'url':url,'author':'另一作者','game_version':'2.2','title':'历史角色甲手法'}
        return doc,ref

    def test_hold_remains_metadata_only_and_audit_does_not_leak_held_text(self):
        m={'schema':'guide-quality-v1','source_id':'g','source_revision':1,'source_sha256':fingerprint(self.fx.source),'status':'pending_verification',
           'issues':[{'code':'key_binding','source_path':self.fx.manifest['source_path'],'quote':'甲A3'}]}
        held=build_quality_page(self.fx.source,self.fx.page,m);self.install(held)
        page=build_followup_page(self.fx.source,held,self.manifest());self.install(page)
        result=self.fx.search()[0]
        self.assertEqual(result['quality_review']['status'],'pending_verification')
        self.assertFalse(result['followup_review']['original_issue_resolved'])
        self.assertNotIn('甲A3',str(result));self.assertNotIn('guide_context',result)
        self.assertNotIn('quote',str(result['followup_review']))

    def test_bad_pin_fabricated_quote_unsafe_reference_or_resolution_rejected(self):
        for kind in ('pin','quote','url','resolved','date'):
            m=self.manifest()
            if kind=='pin':m['source_sha256']='wrong'
            elif kind=='quote':m['checks'][0]['quote']='原文不存在'
            elif kind=='url':m['checks'][0]['url']='http://localhost/private'
            elif kind=='resolved':m['original_issue_resolved']=True
            else:m['checked_at']='not-a-date'
            with self.assertRaises(WikiError,msg=kind):build_followup_page(self.fx.source,self.fx.page,m)

    def test_normal_compile_cannot_silently_delete_audit(self):
        page=build_followup_page(self.fx.source,self.fx.page,self.manifest());self.install(page)
        removed=deepcopy(page);removed.pop('guide_followup')
        draft=self.fx.store.stage_page(removed);self.assertFalse(draft['valid']);self.assertIn('不能删除补证',str(draft['errors']))

    def test_historical_scope_is_opt_in_and_never_substitutes_for_requested_version(self):
        self.history();search=self.fx.fx.search
        self.assertFalse(any(p['page_id']=='page/h0' for p in search('手法',category='guide')))
        self.assertTrue(any(p['page_id']=='page/h0' for p in search('历史 手法',category='guide')))
        self.assertTrue(any(p['page_id']=='page/h0' for p in search('手法',category='guide',include_historical=True)))
        self.assertFalse(any(p['page_id']=='page/h0' for p in search('历史 手法',category='guide',include_historical=False)))
        for query in ('不要历史攻略，只查手法','不用旧版手法','旧稿不能用来顶替，只查手法'):
            self.assertFalse(any(p['page_id']=='page/h0' for p in search(query,category='guide')),query)
        self.assertTrue(any(p['page_id']=='page/h0' for p in search('手法',category='guide',applicable_game_version='2.2')))
        self.assertFalse(search('手法',category='guide',applicable_game_version='3.7'))
        self.assertFalse(search('手法',category='guide',applicable_game_version='3.7',include_historical=True))

    def test_historical_reference_checks_target_and_invalidates_on_update(self):
        doc,ref=self.history();m=self.manifest();m['historical_pages']=[ref]
        page=build_followup_page(self.fx.source,self.fx.page,m);self.install(page)
        bad=deepcopy(page);bad['guide_followup']['historical_pages'][0]['game_version']='3.7'
        self.assertFalse(self.fx.store.stage_page(bad)['valid'])
        doc['document_version']='history2';doc['content']['baseTexts'][0]['introductionDetail']='历史手法：角色甲E→R→Q→退场。'
        import_document(self.fx.store,'h',doc,origin=ref['url'],entities=['character:1'])
        current=self.fx.store.get_page('page/g0',game_version='documents-v1',allow_stale=True)
        self.assertTrue(current['dirty'])

    def test_closure_requires_a_new_original_revision_and_reviewed_repair(self):
        m=self.manifest();m.update(status='resolved',original_issue_resolved=True,supersedes_source_revision=1,resolution='已核对新原文流程。')
        with self.assertRaises(WikiError):build_followup_page(self.fx.source,self.fx.page,m)
        doc=deepcopy(self.fx.doc);doc['document_version']='fixed';doc['content']['baseTexts'][0]['introductionDetail']='修复手法：角色甲A→E→退场'
        import_document(self.fx.store,'g',doc,origin='https://mcguide.kurogames.com/',entities=['character:1'])
        publish_reviewed(self.fx.store,self.fx.fx.compile('g')['draft_id'])
        source=self.fx.store.get_source('g',game_version='documents-v1');page=self.fx.store.get_page('page/g0',game_version='documents-v1')['page']
        m.update(source_revision=2,source_sha256=fingerprint(source));m['checks']=[{'kind':'local_crosscheck','source_path':self.fx.manifest['source_path'],'quote':'修复手法：角色甲A→E→退场','note':'新原篇明确给出修复手法。'}]
        with self.assertRaises(WikiError):build_followup_page(source,page,m)
        c=deepcopy(self.fx.manifest);c.update(source_revision=2,source_sha256=fingerprint(source),terms=[],unresolved_terms=[],coverage_review={'status':'reviewed_text','reviewed_paths':[c['source_path']],'note':'新原篇动作已核对。'},rotations=[{'id':'fixed','format':'source-blocks','mode':'原文手法','chain_scope':'原文未标明链数','context':[],'phases':['修复手法'],'blocks':[{'phase':'修复手法','lines':['修复手法：角色甲A→E→退场']}]}])
        repaired=build_page(source,page,c,self.fx.kb);closed=build_followup_page(source,repaired,m)
        self.install(closed)
        self.assertTrue(self.fx.store.get_page('page/g0',game_version='documents-v1')['page']['guide_followup']['original_issue_resolved'])


if __name__=='__main__':unittest.main()
