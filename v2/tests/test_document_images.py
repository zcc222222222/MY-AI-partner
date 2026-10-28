"""Original media provenance, publication gating, safe delivery and bounded tool output."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
import test_dated_documents as fixtures
from wiki_test_helpers import publish_reviewed
from llmwiki import WikiStore
from llmwiki.collector import DOCUMENT_SCOPE, import_document
from llmwiki.images import extract_images, public_image_url, resolve_image, IMAGE_ROUTE
from llmwiki import tools
from app.services.chat_service import _image_suffix

PIC='https://guide-res.aki-game.com/introduction/material/test/weapon.png'
PIC2='https://guide-res.aki-game.com/introduction/material/test/teammate.png'


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.fx=fixtures.DocumentTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.store=self.fx.store
        self.kb=Path(self.fx.tmp.name)/'kb.db'
        with closing(sqlite3.connect(self.kb)) as c,c:
            c.executescript("CREATE TABLE characters(id,name); INSERT INTO characters VALUES(1,'角色甲'),(2,'角色乙'); CREATE TABLE aliases(entity_type,entity_id,alias);")
        self.doc={'schema':'dated-source-v1','kind':'author_guide','title':'角色甲攻略','author':'作者甲',
                  'source_date':'2026-07-29T12:00:00+08:00','date_basis':'platform_modifiedAt','game_version':None,
                  'document_version':'one','content':{
                      'baseTexts':[{'language':'zh-Hans','introductionDetail':'武器推荐刀甲。配队推荐角色乙。'}],
                      'weapon':{'items':[{'pictureUrl':PIC,'texts':[{'language':'zh-Hans','name':'刀甲'}]}]},
                      'teammate':{'items':[{'main':{'cardPictureUrl':PIC2,'texts':[{'language':'zh-Hans','name':'角色乙'}]}}]}}}

    def source(self):
        return import_document(self.store,'guide',self.doc,origin='https://mcguide.kurogames.com/',entities=['character:1'])

    def publish(self):
        self.source()
        draft=self.fx.compile('guide')
        publish_reviewed(self.store,draft['draft_id'])
        return draft

    def image_ids(self):
        with self.store.connection() as c:
            return [r[0] for r in c.execute('SELECT image_id FROM document_images ORDER BY image_id')]

    def search(self,query='武器',**kw):
        with patch.object(tools,'WIKI_PATH',self.store.path),patch.object(tools,'KNOWLEDGE_PATH',self.kb):
            return tools.search_documents(query,category='guide',entity_name='角色甲',**kw)

    def test_image_urls_only_known_raster_hosts_and_paths(self):
        self.assertEqual(public_image_url(PIC),PIC)
        for bad in ('http://guide-res.aki-game.com/introduction/material/x.png','https://evil.example/x.png',
                    'https://guide-res.aki-game.com.evil.example/introduction/material/x.png',
                    'https://u:p@guide-res.aki-game.com/introduction/material/x.png',
                    'https://guide-res.aki-game.com:443/introduction/material/x.png',
                    'https://guide-res.aki-game.com/introduction/material/../x.png',
                    'https://guide-res.aki-game.com/introduction/material/x.svg',
                    'data:image/png;base64,evil','/api/private.png'):
            self.assertIsNone(public_image_url(bad),bad)

    def test_inline_and_material_paths_preserve_actual_field_and_ordinal(self):
        doc=json.loads(json.dumps(self.doc))
        html='<p>配装段</p><img data-src="'+PIC+'" alt="武器原图"><script><img src="'+PIC2+'"></script>'
        doc['content']['baseTexts'][0]['introductionDetail']=html
        assets=extract_images('g',1,doc,'https://mcguide.kurogames.com/')
        inline=next(a for a in assets if a['kind']=='article_image')
        self.assertEqual(inline['source_path'],['content','baseTexts',0,'introductionDetail'])
        self.assertEqual(inline['image_ordinal'],0)
        self.assertIn('配装段',inline['nearby_text'])
        teammate=next(a for a in assets if a['kind']=='teammate_material')
        self.assertEqual(teammate['source_path'][-1],'cardPictureUrl')
        self.assertEqual(sum(a['kind']=='article_image' for a in assets),1)

    def test_unreviewed_images_not_delivered_or_resolvable(self):
        self.source()
        self.assertTrue(self.image_ids())
        self.assertIsNone(resolve_image(self.store,self.image_ids()[0]))
        self.assertEqual(self.search()['status'],'no_reviewed_match')

    def test_data_endpoint_is_not_exposed_as_readable_source(self):
        self.doc['detail_url']='https://guide-server.aki-game.com/introduction/info?roleGbId=1210&id=10204'
        self.publish()
        expected='https://mcguide.kurogames.com/?role_id=1210&strategy_id=10204'
        result=self.search()
        self.assertEqual(result['results'][0]['document_metadata']['origin'],expected)
        self.assertEqual(result['results'][0]['source_refs'][0]['origin'],expected)
        self.assertEqual(resolve_image(self.store,self.image_ids()[0])['origin'],expected)
        from llmwiki.source_links import readable_source_url
        self.assertIsNone(readable_source_url('https://guide-server.aki-game.com/introduction/info?roleGbId=1210&id=bad'))
        self.assertIsNone(readable_source_url('https://guide-server.aki-game.com:443/introduction/info?roleGbId=1210&id=10204'))
        self.assertIsNone(readable_source_url('https://user:secret@guide-server.aki-game.com/introduction/info?roleGbId=1210&id=10204'))
        # Stored acquisition provenance remains unchanged.
        self.assertIn('guide-server.aki-game.com', self.store.get_source('guide',game_version=DOCUMENT_SCOPE)['text'])

    def test_tool_association_and_suffix_keep_provenance_and_budget(self):
        self.publish()
        r=self.search()
        self.assertEqual(r['status'],'found')
        self.assertLessEqual(len(json.dumps(r,ensure_ascii=False)),8000)
        assets=[a for page in r['results'] for a in page.get('images',[])]
        self.assertTrue(assets)
        self.assertLessEqual(len(assets),2)
        for a in assets:
            self.assertEqual(a['display_url'],IMAGE_ROUTE+a['image_id'])
            self.assertEqual(a['source_id'],'guide')
        sources=[{'source_id':'guide','source_revision':1,'images':assets}]
        with patch.object(tools,'WIKI_PATH',self.store.path):
            suffix=_image_suffix(sources,'正文')
            self.assertIn('![',suffix)
            self.assertIn('作者甲',suffix)
            self.assertIn('平台更新',suffix)
            self.assertIn('查看来源',suffix)
            self.assertEqual(_image_suffix([{'source_id':'other','source_revision':1,'images':assets}],'正文'),'')

    def test_changed_source_and_withdrawal_invalidate_previous_images(self):
        self.publish()
        old=self.image_ids()[0]
        self.assertIsNotNone(resolve_image(self.store,old))
        self.doc['document_version']='two'
        self.source()
        self.assertIsNone(resolve_image(self.store,old))
        self.assertTrue(all(resolve_image(self.store,i) is None for i in self.image_ids()))
        publish_reviewed(self.store,self.fx.compile('guide')['draft_id'])
        newest=[i for i in self.image_ids() if resolve_image(self.store,i)]
        self.assertTrue(newest)
        self.store.delete_source('guide',game_version=DOCUMENT_SCOPE)
        self.assertIsNone(resolve_image(self.store,newest[0]))

    def test_material_images_follow_question_topic_even_without_image_keyword(self):
        self.publish()
        for query,kind in (('武器推荐','weapon_material'),('配队推荐','teammate_material'),('武器原图','weapon_material')):
            result=self.search(query)
            assets=[a for p in result['results'] for a in p.get('images',[])]
            self.assertTrue(assets,query)
            self.assertEqual({a['kind'] for a in assets},{kind},query)

    def test_rejected_latest_review_and_tampered_index_cannot_serve_images(self):
        report=self.publish()
        image_id=self.image_ids()[0]
        with self.store.connection(write=True) as c:
            payload=json.loads(c.execute('SELECT payload FROM document_images WHERE image_id=?',(image_id,)).fetchone()[0])
            payload['url']='https://guide-res.aki-game.com/introduction/material/other.png'
            c.execute('UPDATE document_images SET payload=? WHERE image_id=?',(json.dumps(payload),image_id))
        self.assertIsNone(resolve_image(self.store,image_id))
        another=self.image_ids()[1]
        self.store.review(report['draft_id'],decision='rejected',reviewer='fixture',reviewer_type='agent',note='Withdraw approval')
        self.assertIsNone(resolve_image(self.store,another))

    def test_explicit_image_query_does_not_bypass_author_version_or_entity(self):
        self.doc['content']['role']={'illustrationPictureUrl':PIC,'texts':[{'language':'zh-Hans','name':'角色甲'}]}
        self.publish()
        r=self.search('原图')
        self.assertEqual(r['status'],'found')
        self.assertTrue(any(a['kind']=='character_illustration' for p in r['results'] for a in p.get('images',[])))
        for kw in ({'game_version':'3.8'},{'author':'其他作者'},{'updated_on':'2026-07-28'}):
            result=self.search('原图',**kw)
            self.assertEqual(result['status'],'no_reviewed_match')
            self.assertFalse(result['results'])
        with patch.object(tools,'WIKI_PATH',self.store.path),patch.object(tools,'KNOWLEDGE_PATH',self.kb):
            self.assertEqual(tools.search_documents('原图',category='guide',entity_name='角色乙')['status'],'no_reviewed_match')

    def test_endpoint_auth_validation_and_redirect_without_network(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api import knowledge_images
        from app.auth import current_user
        self.publish()
        app=FastAPI()
        app.include_router(knowledge_images.router)
        image_id=self.image_ids()[0]
        with patch.object(knowledge_images,'WIKI_PATH',self.store.path),TestClient(app) as client:
            self.assertEqual(client.get(IMAGE_ROUTE+image_id,follow_redirects=False).status_code,401)
            app.dependency_overrides[current_user]=lambda: object()
            response=client.get(IMAGE_ROUTE+image_id,follow_redirects=False)
            self.assertEqual(response.status_code,302)
            self.assertIn(response.headers['location'],(PIC,PIC2))
            self.assertEqual(response.headers['cache-control'],'no-store')
            self.assertEqual(client.get(IMAGE_ROUTE+'f'*64,follow_redirects=False).status_code,404)

    def test_actual_agent_tool_delivery_carries_images_to_persistable_suffix(self):
        from app.agents import orchestrator, tools as agent_tools
        self.publish()
        calls=[]
        def stream(messages,**kwargs):
            calls.append(messages)
            if len(calls)==1:
                call={'id':'images','type':'function','function':{'name':'search_documents','arguments':json.dumps({'query':'武器','category':'guide','entity_name':'角色甲'})}}
                return iter([{'type':'tool_calls','tool_calls':[call]},{'type':'completion','finish_reason':'tool_calls'}])
            return iter([{'type':'delta','content':'作者推荐刀甲。'},{'type':'completion','finish_reason':'stop'}])
        with patch.object(tools,'WIKI_PATH',self.store.path),patch.object(tools,'KNOWLEDGE_PATH',self.kb),patch.object(orchestrator,'stream_completion',side_effect=stream):
            events=list(orchestrator.run_agent([{'role':'system','content':'persona unchanged'},{'role':'user','content':'武器'}],tools=agent_tools.TOOLS))
            sources=[s for e in events if e['type']=='knowledge_trace' for s in e['sources']]
            self.assertTrue(any(s.get('images') for s in sources))
            self.assertIn(IMAGE_ROUTE,_image_suffix(sources,'作者推荐刀甲。'))

    def test_bwiki_original_images_are_stable_against_countdown_but_change_with_image(self):
        from llmwiki.sources import bwiki_doc
        url='https://patchwiki.biligame.com/images/wutheringwaves/a/ab/test.png'
        def doc(days,image=url):
            return bwiki_doc('首页/逆境深塔',f'<p>深境干扰</p><img src="{image}"><p>{days}天2时后结束</p>')
        one,two=doc(2),doc(1)
        self.assertEqual(one['document_version'],two['document_version'])
        assets=extract_images('bwiki/test',1,one,'https://wiki.biligame.com/wutheringwaves/首页')
        self.assertEqual(assets[0]['url'],url)
        self.assertNotEqual(one['document_version'],doc(2,url.replace('test.png','other.png'))['document_version'])


if __name__=='__main__':unittest.main()
