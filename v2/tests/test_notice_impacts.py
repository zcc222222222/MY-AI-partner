"""Official changes must retain exact source spans, approval, version and review-only relations."""
from copy import deepcopy
from contextlib import closing
import json
import sqlite3
import unittest
from unittest.mock import patch
import test_dated_documents as fixtures
from wiki_test_helpers import publish_reviewed
from llmwiki import WikiError,tools
from llmwiki.notice_impacts import build_page,fingerprint,search_impacts,candidates
from llmwiki.collector import import_document


class NoticeImpactTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.DocumentTests();self.fx.setUp();self.addCleanup(self.fx.doCleanups)
        self.kb=self.fx.store.path.parent/'knowledge.db'
        with closing(sqlite3.connect(self.kb)) as conn,conn:
            conn.executescript("CREATE TABLE characters(id,name); INSERT INTO characters VALUES(1,'角色甲'),(2,'角色乙'); CREATE TABLE aliases(entity_type,entity_id,alias); CREATE TABLE weapons(id,name); INSERT INTO weapons VALUES(10,'武器甲'); CREATE TABLE echoes(id,name); CREATE TABLE sonatas(id,name);")
        for key,value in (('KNOWLEDGE_PATH',self.kb),('WIKI_PATH',self.fx.store.path)):
            p=patch.object(tools,key,value);p.start();self.addCleanup(p.stop)
        self.text='【问题修复】\n修复了角色甲在特定场景下触发时机异常的问题。\n修复了角色乙的模型显示异常问题。\n修复了武器甲在特定切换操作下效果未生效的问题。'
        self.doc=self.fx.source('notice',text=self.text,kind='official_notice',title='3.7更新公告',game_version='3.7',source_date='2026-09-30',date_basis='official_startTime')
        draft=self.fx.compile('notice');publish_reviewed(self.fx.store,draft['draft_id'])
        self.source=self.fx.store.get_source('notice',game_version='documents-v1');self.page=self.fx.store.get_page('page/notice0',game_version='documents-v1')['page']
        self.manifest={'schema':'notice-impacts-v1','source_id':'notice','source_revision':1,'source_sha256':fingerprint(self.source),'game_version':'3.7','coverage':'reviewed_spans_only','entries':[]}
        for key,kind,line,etype,eid,name in [('timing','character_mechanics',1,'character','1','角色甲'),('model','character_presentation',2,'character','2','角色乙'),('weapon','weapon_effect',3,'weapon','10','武器甲')]:
            quote=self.text.splitlines()[line];self.manifest['entries'].append({'id':key,'kind':kind,'subject':name,'start':self.text.index(quote),'quote':quote,'entities':[{'type':etype,'id':eid,'name':name}]})

    def install(self,manifest=None):
        draft=self.fx.store.stage_page(build_page(self.source,self.page,manifest or self.manifest));self.assertTrue(draft['valid'],draft)
        publish_reviewed(self.fx.store,draft['draft_id']);return draft

    def test_exact_pins_full_spans_and_entity_bindings(self):
        for mutation in ('hash','revision','version','quote','id','line','coverage'):
            bad=deepcopy(self.manifest)
            if mutation=='hash':bad['source_sha256']='bad'
            elif mutation=='revision':bad['source_revision']=2
            elif mutation=='version':bad['game_version']='3.8'
            elif mutation=='quote':bad['entries'][0]['quote']='修复了角色甲。'
            elif mutation=='id':bad['entries'][0]['entities'][0]['id']='2'
            elif mutation=='line':bad['entries'][0]['quote']=bad['entries'][0]['quote'][:-1]
            else:bad['coverage']='whole_notice'
            with self.subTest(mutation=mutation),self.assertRaises(WikiError):build_page(self.source,self.page,bad)

    def test_candidates_are_unreviewed_and_staging_does_not_publish(self):
        self.assertTrue(all(c['status']=='needs_source_review' for c in candidates(self.source)))
        self.fx.store.stage_page(build_page(self.source,self.page,self.manifest))
        self.assertEqual(search_impacts(self.fx.store,game_version='3.7')['results'],[])

    def test_visual_changes_are_not_mechanics_review_and_version_is_exact(self):
        self.install()
        a=search_impacts(self.fx.store,game_version='3.7',entity='character:1')['results'][0]
        b=search_impacts(self.fx.store,game_version='3.7',entity='character:2')['results'][0]
        self.assertTrue(a['review_required']);self.assertFalse(b['review_required']);self.assertFalse(b['related_pages'])
        self.assertEqual(search_impacts(self.fx.store,game_version='3.8')['results'],[])

    def test_weapon_mentions_link_to_character_guide_without_invalidating_it(self):
        doc={'schema':'dated-source-v1','kind':'author_guide','title':'角色乙攻略','author':'作者','source_date':'2026-08-01','date_basis':'platform_modifiedAt','game_version':None,'document_version':'v1','content':{'baseTexts':[{'language':'zh-Hans','introductionDetail':'角色乙的候选武器为武器甲。'}]}}
        import_document(self.fx.store,'guide',doc,origin='https://example.com/guide',entities=['character:2'])
        publish_reviewed(self.fx.store,self.fx.compile('guide')['draft_id']);before=self.fx.store.get_page('page/guide0',game_version='documents-v1')
        self.install();result=search_impacts(self.fx.store,game_version='3.7',entity='character:2')
        weapon=next(x for x in result['results'] if x['id']=='weapon')
        self.assertEqual(weapon['related_pages'][0]['relation'],'mentions_changed_equipment')
        self.assertEqual(weapon['related_pages'][0]['status'],'needs_review')
        by_weapon=search_impacts(self.fx.store,game_version='3.7',entity='weapon:10')
        self.assertEqual(by_weapon['results'][0]['related_pages'][0]['page_id'],'page/guide0')
        after=self.fx.store.get_page('page/guide0',game_version='documents-v1');self.assertEqual(before,after)

    def test_new_source_revision_invalidates_old_change_evidence(self):
        self.install();self.doc['readable_text']+='\n补充说明。';self.doc['document_version']='v2'
        import_document(self.fx.store,'notice',self.doc,origin='https://example.com')
        self.assertFalse(search_impacts(self.fx.store,game_version='3.7')['results'])

    def test_withdrawn_approval_is_not_player_evidence(self):
        draft=self.install();self.fx.store.review(draft['draft_id'],decision='rejected',reviewer='checker',note='需重新核对')
        self.assertFalse(search_impacts(self.fx.store,game_version='3.7')['results'])

    def test_tool_does_not_relax_unknown_entity_or_version(self):
        self.install()
        self.assertEqual(tools.get_version_impact()['error'],'version_required')
        self.assertEqual(tools.get_version_impact('3.7','不存在')['error'],'entity_unresolved')
        self.assertEqual(tools.get_version_impact('3.7','角色甲')['status'],'found')
        self.assertEqual(tools.get_version_impact('3.8','角色甲')['status'],'no_reviewed_match')

    def test_query_date_excludes_announcements_not_yet_published(self):
        self.install()
        self.assertEqual(tools.get_version_impact('3.7','角色甲',as_of='2026-09-29')['status'],'no_reviewed_match')
        self.assertEqual(tools.get_version_impact('3.7','角色甲',as_of='2026-09-30')['status'],'found')

    def test_source_revision_comparison_is_not_a_game_version_delta(self):
        bad=deepcopy(self.source);data=json.loads(bad['text']);data['kind']='author_guide';bad['text']=json.dumps(data)
        bad_manifest=deepcopy(self.manifest);bad_manifest['source_sha256']=fingerprint(bad)
        with self.assertRaises(WikiError):build_page(bad,self.page,bad_manifest)

    def test_budget_keeps_complete_change_quotes_and_reports_partial_coverage(self):
        m=deepcopy(self.manifest);m['entries']=[{**m['entries'][0],'id':'timing-'+str(i)} for i in range(70)]
        self.install(m);r=search_impacts(self.fx.store,game_version='3.7',entity='character:1',limit=8)
        self.assertLessEqual(len(json.dumps(r,ensure_ascii=False)),8000);self.assertFalse(r['complete'])
        self.assertTrue(all(e['quote']==self.manifest['entries'][0]['quote'] for e in r['results']))


if __name__=='__main__':unittest.main()
