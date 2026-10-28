"""Impact review lifecycle, immutable pins, partial scope and real revision closure."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch
import test_notice_impacts as fixtures
from llmwiki import WikiError
from llmwiki.collector import import_document
from llmwiki import impact_reviews as reviews
from wiki_test_helpers import publish_reviewed


class ImpactReviewTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.NoticeImpactTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups);self.fx.install()
        self.store=self.fx.fx.store
        self.doc={'schema':'dated-source-v1','kind':'author_guide','title':'角色甲攻略','author':'作者','source_date':'2026-08-01',
            'date_basis':'platform_modifiedAt','game_version':None,'document_version':'v1','content':{'baseTexts':[{'language':'zh-Hans','introductionDetail':'角色甲的触发需满足特定场景条件。'}]}}
        import_document(self.store,'guide',self.doc,origin='https://example.com/guide',entities=['character:1'])
        publish_reviewed(self.store,self.fx.fx.compile('guide')['draft_id'])

    def record(self,state='insufficient_evidence'):
        r=reviews.template(self.store,'page/notice0','timing','page/guide0','documents-v1')
        p=self.store.get_page('page/guide0',game_version='documents-v1')['page'];c=next(c for c in p['claims'] if '触发' in c['text'])
        r['checks']=[{'claim_id':c['claim_id'],'claim_sha256':reviews.sha(c),'outcome':state,'note':'仅核对这一条，未确认实战适用。'}]
        r['summary']='本轮已核对所列结论，未验证整篇或伤害。';return r

    def approve(self,r=None):
        staged=reviews.stage(self.store,r or self.record())
        reviews.decide(self.store,staged['stage_id'],decision='approved',reviewer='fixture-agent',note='核对人工构造证据及范围')
        return staged['stage_id']

    def test_pending_is_not_public_and_repeat_is_idempotent(self):
        r=self.record();a=reviews.stage(self.store,r);self.assertFalse(reviews.index(self.store))
        b=reviews.stage(self.store,r);self.assertEqual(a['stage_id'],b['stage_id']);self.assertTrue(b['unchanged'])
        self.approve(r);self.assertEqual(next(iter(reviews.index(self.store).values()))['status'],'insufficient_evidence')
        with self.store.connection() as conn:generation=conn.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0]
        self.assertTrue(reviews.decide(self.store,a['stage_id'],decision='approved',reviewer='same',note='相同状态不重复更新')['unchanged'])
        with self.store.connection() as conn:self.assertEqual(generation,conn.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0])

    def test_does_not_change_target_and_no_change_is_scoped(self):
        before=self.store.get_page('page/guide0',game_version='documents-v1');self.approve(self.record('no_text_change'))
        self.assertEqual(before,self.store.get_page('page/guide0',game_version='documents-v1'))
        item=reviews.queue(self.store)['items'];item=next(i for i in item if i['page_id']=='page/guide0')
        self.assertEqual(item['status'],'no_text_change');self.assertEqual(item['assessment_review']['coverage'],'listed_claims_only')

    def test_bad_page_source_or_claim_pin_cannot_be_approved(self):
        for field in ('target','notice','claim'):
            r=self.record()
            if field=='target':r['target']['sha256']='wrong'
            elif field=='notice':r['notice']['change_sha256']='wrong'
            else:r['checks'][0]['claim_sha256']='wrong'
            with self.subTest(field=field),self.assertRaises(WikiError):reviews.stage(self.store,r)

    def test_target_source_change_reopens_and_blocks_delayed_approval(self):
        sid=self.approve();self.doc['document_version']='v2';self.doc['content']['baseTexts'][0]['introductionDetail']='角色甲的新触发条件。'
        import_document(self.store,'guide',self.doc,origin='https://example.com/guide',entities=['character:1'])
        state=next(iter(reviews.index(self.store).values()));self.assertTrue(state['reopened']);self.assertFalse(state['checks'])
        with self.assertRaises(WikiError):reviews.decide(self.store,sid,decision='approved',reviewer='agent',note='不能批准过期记录')

    def test_notice_change_preserves_reopen_alert_when_page_dirty(self):
        self.approve();doc=deepcopy(self.fx.doc);doc['document_version']='v2';doc['readable_text']+='\n新补充。'
        import_document(self.store,'notice',doc,origin='https://example.com')
        alerts=reviews.queue(self.store)['items'];self.assertTrue(any(x.get('reopened') for x in alerts))

    def test_target_page_republication_reopens_even_if_source_unchanged(self):
        self.approve();p=self.store.get_page('page/guide0',game_version='documents-v1')['page'];p['open_questions']=['新范围待核对']
        p.pop('base_revision',None);publish_reviewed(self.store,self.store.stage_page(p)['draft_id'])
        self.assertEqual(next(iter(reviews.index(self.store).values()))['status'],'needs_review')

    def test_revocation_and_historical_asof_do_not_use_later_decisions(self):
        sid=self.approve();self.assertFalse(reviews.index(self.store,as_of='2020-01-01T00:00:00+00:00'))
        reviews.decide(self.store,sid,decision='rejected',reviewer='agent',note='撤回复核')
        self.assertEqual(next(iter(reviews.index(self.store).values()))['status'],'needs_review')

    def test_revision_requires_approved_need_and_wiki_review_before_closure(self):
        sid=self.approve(self.record('revision_required'));r=self.record();cid=r['checks'][0]['claim_id']
        draft=reviews.stage_revision(self.store,sid,{cid:'作者攻略观点：角色甲的触发需要满足特定场景条件。'})
        self.assertTrue(draft['valid'],draft)
        self.assertEqual(next(iter(reviews.index(self.store).values()))['status'],'revision_required')
        with self.assertRaises(WikiError):self.store.publish(draft['draft_id'])
        publish_reviewed(self.store,draft['draft_id'])
        closed=self.record('no_text_change');closed.update(kind='revision_published',supersedes=sid)
        self.approve(closed)
        self.assertEqual(next(iter(reviews.index(self.store).values()))['status'],'revision_published')

    def test_faked_revision_completion_and_out_of_scope_edits_rejected(self):
        sid=self.approve(self.record('revision_required'));fake=self.record('no_text_change');fake.update(kind='revision_published',supersedes=sid)
        with self.assertRaises(WikiError):reviews.stage(self.store,fake)
        with self.assertRaises(WikiError):reviews.stage_revision(self.store,sid,{'other':'新增未核实结论'})

    def test_unrelated_target_cannot_get_an_assessment(self):
        with self.assertRaises(WikiError):reviews.template(self.store,'page/notice0','weapon','page/guide0','documents-v1')

    def test_ordinary_guide_retrieval_carries_partial_review_only_for_delivered_claims(self):
        from llmwiki.tools import search_documents
        self.approve();result=search_documents('角色甲 触发',category='guide',entity_name='1')
        page=next(p for p in result['results'] if p['page_id']=='page/guide0')
        self.assertEqual(page['impact_reviews'][0]['status'],'insufficient_evidence')
        self.assertEqual(page['impact_reviews'][0]['review']['coverage'],'listed_claims_only')
        self.assertEqual({s['source_id'] for s in page['source_refs']},{'guide'})
        ordinary=[{'page_id':'page/guide0','matches':[{'claim_id':'unrelated'}]}]
        reviews.page_summaries(self.store,ordinary,'documents-v1');self.assertNotIn('impact_reviews',ordinary[0])

    def test_update_plan_contains_separate_impact_queue(self):
        from llmwiki.updates import plan_documents
        before=plan_documents(self.store);self.approve();plan=plan_documents(self.store)
        self.assertEqual(plan['impact_reviews']['counts']['insufficient_evidence'],1)
        self.assertEqual(plan['pages'],before['pages'])

    def test_audit_integrity_failure_reopens_and_never_reuses_previous_conclusion(self):
        sid=self.approve()
        with self.store.connection(write=True) as conn:
            payload=json.loads(conn.execute('SELECT payload FROM events WHERE event_id=?',(sid,)).fetchone()[0])
            payload['record']['checks'][0]['outcome']='no_text_change'
            conn.execute('UPDATE events SET payload=? WHERE event_id=?',(json.dumps(payload,ensure_ascii=False),sid))
        state=next(iter(reviews.index(self.store).values()));self.assertTrue(state['reopened']);self.assertFalse(state['checks'])

    def test_page_reapproval_cannot_close_a_required_revision_without_text_changes(self):
        sid=self.approve(self.record('revision_required'))
        page=self.store.get_page('page/guide0',game_version='documents-v1')['page'];page.pop('base_revision',None);page['open_questions']=['变化但结论未修订']
        publish_reviewed(self.store,self.store.stage_page(page)['draft_id'])
        closed=self.record('no_text_change');closed.update(kind='revision_published',supersedes=sid)
        with self.assertRaises(WikiError):reviews.stage(self.store,closed)

    def test_closed_scoped_assessment_is_visible_in_impact_query(self):
        from llmwiki.tools import get_version_impact
        self.approve(self.record('no_text_change'));result=get_version_impact('3.7','角色甲')
        item=next(e for e in result['results'] if e['id']=='timing')
        self.assertFalse(item['review_required']);self.assertEqual(item['related_pages'][0]['status'],'no_text_change')

    def test_ordinary_retrieval_projects_mixed_decisions_to_returned_claims(self):
        record=self.record('insufficient_evidence');page=self.store.get_page('page/guide0',game_version='documents-v1')['page']
        other=next(c for c in page['claims'] if c['claim_id']!=record['checks'][0]['claim_id'])
        record['checks'].append({'claim_id':other['claim_id'],'claim_sha256':reviews.sha(other),'outcome':'no_text_change','note':'仅此项无需改文。'})
        self.approve(record);results=[{'page_id':'page/guide0','matches':[{'claim_id':other['claim_id']}]}]
        reviews.page_summaries(self.store,results,'documents-v1');review=results[0]['impact_reviews'][0]
        self.assertEqual(review['status'],'no_text_change');self.assertEqual(review['review']['record_status'],'insufficient_evidence')
        self.assertEqual(len(review['review']['checked_claims']),1)

    def test_concurrent_target_publish_cannot_be_overwritten_by_revision_draft(self):
        sid=self.approve(self.record('revision_required'));cid=self.record()['checks'][0]['claim_id'];original=self.store.stage_page
        def race(payload):
            changed=self.store.get_page('page/guide0',game_version='documents-v1')['page'];changed.pop('base_revision',None)
            changed['open_questions']=['并发更新需要保留'];publish_reviewed(self.store,original(changed)['draft_id'])
            return original(payload)
        with patch.object(self.store,'stage_page',side_effect=race):
            draft=reviews.stage_revision(self.store,sid,{cid:'作者攻略观点：角色甲的触发需要满足特定场景条件。'})
        self.assertFalse(draft['valid']);self.assertIn('page_revision_changed',str(draft['errors']))


if __name__=='__main__':unittest.main()
