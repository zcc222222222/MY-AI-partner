"""Session-local intent and actual execution gates, with no network or user DB writes."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
from types import SimpleNamespace as NS

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from app.agents import intent, orchestrator, tools
from app.services import chat_service
from llmwiki import tools as wiki


def raw(topics=None,keys=None,**kw):
    return {"topics":topics or ["build"],"entity_keys":keys or [],"game_version":None,"author":None,
            "as_of":None,"updated_on":None,"ambiguous":False,**kw}


class IntentTests(unittest.TestCase):
    def test_saved_library_scope_survives_parser_outage_without_relaxing_other_filters(self):
        with patch.object(intent, 'extract', side_effect=TimeoutError):
            plan = intent.resolve_intent([{'role':'user','content':'只依据当前收录的角色甲攻略说零链输出轴，摘录不全就说不全。'}])
            self.assertFalse(plan['ambiguous'])
            self.assertTrue(plan['library_only'])
            names = {t['function']['name'] for t in intent.allowed_tools(plan, tools.TOOLS)}
            self.assertIn('search_documents', names)
            self.assertFalse(names & intent.LIVE_TOOLS)
            self.assertNotIn('search_wiki', names)
            with self.assertRaises(ValueError):
                intent.scoped_arguments(plan, 'search_wiki', {'query':'零链'})
            with self.assertRaises(ValueError):
                intent.scoped_arguments(plan, 'find_gameplay_guides', {'query':'角色甲'})
            for restriction in ('只依据未知网站的角色甲攻略', '只依据当前收录的角色甲攻略，作者必须是乙', '只依据当前收录的角色甲攻略，更新于2026-10-01'):
                restricted = intent.resolve_intent([{'role':'user','content':restriction}])
                self.assertTrue(restricted['ambiguous'], restriction)

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.kb=Path(self.tmp.name)/'knowledge.db'
        with closing(sqlite3.connect(self.kb)) as c,c:
            c.executescript("""
                CREATE TABLE aliases(entity_type,entity_id,alias);
                CREATE TABLE characters(id,name);
                INSERT INTO characters VALUES(1,'角色甲'),(2,'角色乙'),(3,'漂泊者·导电'),(4,'漂泊者·气动'),(5,'漂泊者·导电');
                INSERT INTO aliases VALUES('character','1','Alpha');
                CREATE TABLE weapons(id,name); INSERT INTO weapons VALUES(10,'武器甲');
                CREATE TABLE echoes(id,name); CREATE TABLE sonatas(id,name); CREATE TABLE monsters(id,name);
            """)
        self.addCleanup(patch.stopall)
        patch.object(wiki,'KNOWLEDGE_PATH',self.kb).start()

    def resolve(self,text,response=None,history=None):
        messages=[*(history or []),{"role":"user","content":text}]
        with patch.object(intent,'extract',return_value=response or raw(keys=['character:1'])):
            return intent.resolve_intent(messages)

    def test_candidates_are_known_mentions_and_followup_retains_context(self):
        captured=[]
        def extract(payload):
            captured.append(payload)
            return raw(['team'],['character:1'])
        messages=[{'role':'user','content':'Alpha怎么养成？'},{'role':'assistant','content':'可以分武器和配队讨论。'}, {'role':'user','content':'那她配队呢？'}]
        with patch.object(intent,'extract',side_effect=extract): plan=intent.resolve_intent(messages)
        self.assertEqual(plan['entities'][0]['name'],'角色甲')
        self.assertEqual(plan['topics'],['team'])
        self.assertEqual(captured[0]['turns'][-1]['text'],'那她配队呢？')
        self.assertNotIn('character:2',[c['key'] for c in captured[0]['candidates']])

    def test_role_correction_and_sessions_do_not_share_targets(self):
        one=self.resolve('改为角色乙',raw(keys=['character:2']),[{'role':'user','content':'角色甲怎么养成'}])
        two=self.resolve('她呢？',raw(keys=['character:2']))
        self.assertEqual([e['key'] for e in one['entities']],['character:2'])
        self.assertEqual(two['status'],'fallback')
        self.assertFalse(two['entities'])

    def test_rover_form_candidates_are_not_collapsed(self):
        turns=[{'role':'user','text':'漂泊者导电怎么养成'}]
        self.assertEqual({e['key'] for e in intent.candidates(turns)},{'character:3','character:5'})
        plan=self.resolve('漂泊者怎么配队',raw(['team'],['character:3','character:4'],ambiguous=True))
        self.assertTrue(plan['ambiguous'])
        self.assertIsNone(intent.allowed_tools(plan,tools.TOOLS))
        guessed=self.resolve('漂泊者怎么配队',raw(['team'],['character:3']))
        self.assertTrue(guessed['ambiguous'])
        with patch.object(intent,'extract',side_effect=TimeoutError):
            fallback=intent.resolve_intent([{'role':'user','content':'漂泊者怎么配队'}])
        self.assertTrue(fallback['ambiguous'])
        persona=self.resolve('我作为漂泊者向你提问：角色甲推荐武器是什么？',raw(['build'],['character:1']))
        self.assertFalse(persona['ambiguous'])

    def test_image_preferences_persist_and_one_off_override_is_not_permanent(self):
        texts=['角色甲怎么配队','后面不用图','武器呢','给我看看','继续讲配队','恢复自动配图']
        turns=[]
        for text,expected in zip(texts,['auto','none','none','required','none','auto']):
            turns.append({'role':'user','text':text})
            self.assertEqual(intent.image_mode(turns),expected,text)
        self.assertEqual(intent.image_mode([{'role':'user','text':'给我原图，不要图片'}]),'none')
        self.assertEqual(intent.image_mode([{'role':'user','text':'不要再给我发图片，文字就行'}]),'none')
        self.assertEqual(intent.image_mode([{'role':'user','text':'不要配图了，但这次给我原图'}]),'required')

    def test_explicit_version_cannot_be_dropped_or_overridden(self):
        plan=self.resolve('角色甲只查3.8版本',raw(['stats'],['character:1']))
        self.assertEqual(plan['game_version'],'3.8')
        args=intent.scoped_arguments(plan,'search_wiki',{'query':'机制','entity_name':'角色甲','game_version':'3.7'})
        self.assertEqual(args['game_version'],'3.8')
        self.assertEqual(args['entity_name'],'1')
        multiple=self.resolve('对比角色甲3.7和3.8',raw(['stats'],['character:1']))
        self.assertTrue(multiple['ambiguous'])

    def test_role_guide_is_not_an_author_filter_but_explicit_namesake_is_retained(self):
        plan=self.resolve('按角色甲攻略给出完整手法',raw(['rotation'],['character:1'],author='角色甲'))
        self.assertIsNone(plan['author'])
        args=intent.scoped_arguments(plan,'search_documents',{'query':'完整手法','author':'角色甲'})
        self.assertNotIn('author',args)
        for text in ('按作者角色甲写的攻略','角色甲出品的攻略'):
            plan=self.resolve(text,raw(['rotation'],['character:1'],author='角色甲'))
            self.assertEqual(plan['author'],'角色甲')

    def test_role_alias_is_not_mistaken_for_author(self):
        with closing(sqlite3.connect(self.kb)) as conn,conn:
            conn.execute("INSERT INTO aliases VALUES('character','1','角色全称甲')")
        plan=self.resolve('按角色全称甲攻略给手法',raw(['rotation'],['character:1'],author='角色全称甲'))
        self.assertIsNone(plan['author'])
        explicit=self.resolve('按作者角色全称甲写的攻略给手法',raw(['rotation'],['character:1'],author='角色全称甲'))
        self.assertEqual(explicit['author'],'角色全称甲')

    def test_single_character_guide_mentions_inside_natural_sentences(self):
        with closing(sqlite3.connect(self.kb)) as conn,conn:
            conn.executemany('INSERT INTO characters VALUES(?,?)',[(6,'椿'),(7,'心')])
        picked=intent.candidates([{'role':'user','text':'可以给我椿的历史双变奏手法吗？'}])
        self.assertEqual([c['id'] for c in picked],['6'])
        self.assertFalse(any(c['id']=='7' for c in intent.candidates([{'role':'user','text':'鉴心的攻略'}])))

    def test_rejected_old_version_does_not_make_explicit_target_ambiguous(self):
        text='现在换成3.7，只查角色甲手法。刚才的2.2旧稿不能用来顶替。'
        plan=self.resolve(text,raw(['rotation'],['character:1'],game_version='2.2'))
        self.assertEqual(plan['game_version'],'3.7');self.assertFalse(plan['ambiguous'])
        self.assertEqual(intent.requested_versions('不要用2.2，只看3.7'),['3.7'])
        self.assertEqual(intent.requested_versions('对比2.2和3.7'),['2.2','3.7'])

    def test_attribution_request_survives_fallback_but_specific_author_does_not(self):
        with patch.object(intent,'extract',side_effect=TimeoutError()):
            plan=intent.resolve_intent([{'role':'user','content':'给角色甲历史手法，要交代作者、版本和文章展示日期。'}])
        self.assertFalse(plan['ambiguous']);self.assertEqual(plan['entities'][0]['id'],'1')
        with patch.object(intent,'extract',side_effect=TimeoutError()):
            locked=intent.resolve_intent([{'role':'user','content':'只看作者A的角色甲手法。'}])
        self.assertTrue(locked['ambiguous'])

    def test_single_character_history_flow_and_self_declared_version_are_attribution(self):
        with closing(sqlite3.connect(self.kb)) as conn,conn:conn.execute("INSERT INTO characters VALUES(6,'椿')")
        text='请查已入库的椿历史双变奏手法，只说首次入场，说明作者自述版本和日期。'
        self.assertTrue(intent._mentioned('椿',text))
        self.assertFalse(intent._mentioned('心','请查已入库的鉴心历史双变奏手法'))
        with patch.object(intent,'extract',side_effect=TimeoutError()):plan=intent.resolve_intent([{'role':'user','content':text}])
        self.assertFalse(plan['ambiguous']);self.assertEqual(plan['entities'][0]['id'],'6')

    def test_historical_scope_inherits_from_user_and_resets_on_role_switch_or_rejection(self):
        history=[{'role':'user','content':'角色甲历史手法怎么打？'},{'role':'assistant','content':'这是历史资料。'}]
        plan=self.resolve('那第二次入场呢？',raw(['rotation'],['character:1']),history)
        self.assertTrue(plan['include_historical'])
        args=intent.scoped_arguments(plan,'search_documents',{'query':'第二次入场','include_historical':False})
        self.assertTrue(args['include_historical'])
        other=self.resolve('换成角色乙的配队',raw(['team'],['character:2']),history)
        self.assertFalse(other['include_historical'])
        current=self.resolve('旧稿不能用来顶替，只查最新攻略',raw(['rotation'],['character:1']),history)
        self.assertFalse(current['include_historical'])
        poisoned=self.resolve('角色甲的手法',raw(['rotation'],['character:1']),[{'role':'assistant','content':'历史手法是……'}])
        self.assertFalse(poisoned['include_historical'])

    def test_assistant_source_metadata_is_not_a_user_filter(self):
        history=[{'role':'user','content':'角色甲历史手法，说明作者自述版本和日期。'},
                 {'role':'assistant','content':'作者黑棉花悟能，版本2.2，日期2025-09-22。'}]
        plan=self.resolve('那第二次入场呢？',raw(['rotation'],['character:1'],game_version='2.2',author='黑棉花悟能',updated_on='2025-09-22'),history)
        self.assertEqual(plan['status'],'parsed');self.assertIsNone(plan['game_version']);self.assertIsNone(plan['author']);self.assertIsNone(plan['updated_on'])
        self.assertTrue(plan['include_historical'])
        explicit=self.resolve('仍只看作者A的角色甲攻略',raw(['rotation'],['character:1'],author='黑棉花悟能'),history)
        self.assertEqual(explicit['status'],'fallback');self.assertTrue(explicit['ambiguous'])

    def test_author_and_date_lock_and_no_game_version_inferred_from_date(self):
        plan=self.resolve('只依据作者A平台更新于2026年7月9日的角色甲攻略',raw(keys=['character:1'],author='作者A',updated_on='2026-07-09'))
        args=intent.scoped_arguments(plan,'search_documents',{'query':'武器','author':'其他作者','updated_on':'2026-07-10'})
        self.assertEqual(args['author'],'作者A')
        self.assertEqual(args['updated_on'],'2026-07-09')
        self.assertNotIn('game_version',args)
        self.assertNotIn('as_of',args)

    def test_document_date_filter_cannot_be_replaced_with_character_profile(self):
        plan=self.resolve('只查平台更新于2026年7月8日的角色甲攻略',raw(['profile'],['character:1'],updated_on='2026-07-08'))
        names={t['function']['name'] for t in intent.allowed_tools(plan,tools.TOOLS)}
        self.assertIn('search_documents',names)
        self.assertNotIn('get_character_profile',names)
        with self.assertRaises(ValueError):
            intent.scoped_arguments(plan,'get_character_profile',{'name':'角色甲'})

    def test_invented_fields_and_incomplete_parser_do_not_relax_restricted_request(self):
        for response in (raw(keys=['character:999']),raw(keys=['character:1'],game_version='9.9'),raw(author='不存在'),raw(as_of='2025-01-01')):
            with self.subTest(response=response):
                plan=self.resolve('只依据角色甲攻略，平台更新2026-07-09',response)
                self.assertEqual(plan['status'],'fallback')
                self.assertTrue(plan['ambiguous'])
                self.assertIsNone(intent.allowed_tools(plan,tools.TOOLS))

    def test_timeout_degrades_without_disabling_simple_chat(self):
        with patch.object(intent,'extract',side_effect=TimeoutError):
            plan=intent.resolve_intent([{'role':'user','content':'你好，不用图'}])
        self.assertEqual(plan['status'],'fallback')
        self.assertEqual(plan['image_mode'],'none')
        self.assertEqual(intent.allowed_tools(plan,tools.TOOLS),tools.TOOLS)
        with patch.object(intent,'extract',side_effect=TimeoutError):
            versioned=intent.resolve_intent([{'role':'user','content':'角色甲只依据3.8版本资料说配队'}])
        self.assertFalse(versioned['ambiguous'])
        self.assertEqual(versioned['game_version'],'3.8')
        self.assertEqual(versioned['entities'][0]['key'],'character:1')

    def test_routing_selects_distinct_tools_and_mixed_routes(self):
        for topics,expected in ((['team'],{'search_documents'}),(['stats'],{'get_character_stats','get_character_skills','get_knowledge_entity'}),
                                (['team','mechanism'],{'search_documents','search_wiki','get_character_skills','get_entity_relations','get_knowledge_entity'}),(['chat'],set())):
            plan=self.resolve('角色甲',raw(topics,['character:1']))
            names={t['function']['name'] for t in intent.allowed_tools(plan,tools.TOOLS) or []}-intent.UTILITY_TOOLS
            self.assertEqual(names,expected)

    def test_explicit_guide_parry_request_keeps_document_route(self):
        plan=self.resolve('只查角色甲攻略里的格挡反击原文',raw(['mechanism'],['character:1']))
        self.assertIn('rotation',plan['topics'])
        self.assertIn('search_documents',{t['function']['name'] for t in intent.allowed_tools(plan,tools.TOOLS)})
        plain=self.resolve('角色甲格挡反击的机制是什么',raw(['mechanism'],['character:1']))
        self.assertEqual(plain['topics'],['mechanism'])
        negative=self.resolve('角色甲不用查攻略，只解释格挡机制',raw(['mechanism'],['character:1']))
        self.assertEqual(negative['topics'],['mechanism'])
        guide=self.resolve('按收录的角色甲攻略解释3链护盾',raw(['mechanism'],['character:1']))
        self.assertIn('search_documents',{t['function']['name'] for t in intent.allowed_tools(guide,tools.TOOLS)})

    def test_execution_gate_blocks_other_character_even_if_model_calls_it(self):
        plan=self.resolve('角色甲配队',raw(['team'],['character:1']))
        with self.assertRaises(ValueError): intent.scoped_arguments(plan,'search_documents',{'query':'攻略','entity_name':'角色乙'})
        args=intent.scoped_arguments(plan,'search_documents',{'query':'攻略','entity_name':'她'})
        self.assertEqual(args['entity_name'],'1')
        self.assertEqual(args['category'],'guide')

    def test_extractor_has_bounded_non_thinking_json_call_and_complete_finish(self):
        client=Mock()
        client.with_options.return_value=client
        response=NS(choices=[NS(finish_reason='length',message=NS(content='{}'))])
        client.chat.completions.create.return_value=response
        with patch.object(intent.llm,'_get_client',return_value=client),self.assertRaises(ValueError): intent.extract({'turns':[]})
        kwargs=client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs['max_tokens'],512)
        self.assertEqual(kwargs['response_format'],{'type':'json_object'})
        self.assertEqual(kwargs['extra_body'],{'thinking':{'type':'disabled'}})

    def test_early_user_memory_is_unwrapped_and_generated_footers_not_entity_evidence(self):
        from app.services.context_service import NOTICE
        messages=[{'role':'user','content':NOTICE+json.dumps([{'message_id':1,'user_text':'不用图，角色甲'}],ensure_ascii=False)},
                  {'role':'assistant','content':'回答。\n\n相关原图：角色乙'}, {'role':'user','content':'她武器呢？'}]
        turns=intent._turns(messages)
        self.assertEqual(turns[0]['text'],'不用图，角色甲')
        self.assertNotIn('character:2',[e['key'] for e in intent.candidates(turns)])

    def run_routed(self,query,response,tool_name,args,payload=None,history=None):
        snapshots=[]
        def stream(messages,**kw):
            snapshots.append((list(messages),kw))
            if len(snapshots)==1:
                return iter([{'type':'tool_calls','tool_calls':[{'id':'i','function':{'name':tool_name,'arguments':json.dumps(args)}}]}, {'type':'completion','finish_reason':'tool_calls'}])
            return iter([{'type':'delta','content':'完整回答'},{'type':'completion','finish_reason':'stop'}])
        with patch.object(intent,'extract',return_value=response),patch.object(orchestrator,'stream_completion',side_effect=stream), \
             patch.object(orchestrator,'execute_tool',return_value=json.dumps(payload or {'results':[],'status':'found'})) as execute:
            events=list(orchestrator.run_agent([{'role':'system','content':'stable-persona'},*(history or []),{'role':'user','content':query}],tools=tools.TOOLS,intent_routing=True))
        return events,snapshots,execute

    def test_actual_loop_scopes_author_role_category_and_keeps_persona_unchanged(self):
        events,snapshots,execute=self.run_routed('作者A的角色甲配队',raw(['team'],['character:1'],author='作者A'),'search_documents',{'query':'配队','entity_name':'角色甲','category':'official'})
        args=json.loads(execute.call_args.args[1])
        self.assertEqual((args['entity_name'],args['category'],args['author']),('1','guide','作者A'))
        self.assertEqual(snapshots[0][0][0],{'role':'system','content':'stable-persona'})
        self.assertTrue(any(e['type']=='intent_trace' for e in events))

    def test_actual_loop_blocks_unadvertised_tool_and_cross_entity(self):
        for name,args in (('get_character_stats',{'name':'角色甲'}),('search_documents',{'query':'配队','entity_name':'角色乙'})):
            events,snapshots,execute=self.run_routed('角色甲配队',raw(['team'],['character:1']),name,args)
            execute.assert_not_called()
            tool_result=next(m for m in snapshots[1][0] if m['role']=='tool')
            self.assertEqual(json.loads(tool_result['content'])['error'],'intent_scope_mismatch')

    def test_version_blocks_sql_on_first_call_before_any_wiki_search(self):
        with patch.object(orchestrator,'_structured_version',return_value='3.7'):
            events,snapshots,execute=self.run_routed('角色甲3.8面板',raw(['stats'],['character:1']),'get_character_stats',{'name':'角色甲'})
        execute.assert_not_called()
        tool_result=next(m for m in snapshots[1][0] if m['role']=='tool')
        self.assertEqual(json.loads(tool_result['content'])['error'],'knowledge_version_mismatch')

    def test_version_impact_is_routed_scoped_and_cited(self):
        payload={'status':'found','game_version':'3.7','results':[{'title':'更新公告','url':'https://example.com/notice','source_id':'n','source_revision':2,'game_version':'3.7','source_date':'2026-09-30'}]}
        events,snapshots,execute=self.run_routed('3.7更新是否影响角色甲的旧攻略',raw(['version_impact'],['character:1']),'get_version_impact',{'game_version':'3.8','entity_name':'角色甲'},payload)
        args=json.loads(execute.call_args.args[1]);self.assertEqual(args['game_version'],'3.7');self.assertEqual(args['entity_name'],'1')
        trace=next(e for e in events if e['type']=='knowledge_trace');self.assertEqual(trace['sources'][0]['source_id'],'n');self.assertEqual(trace['trace']['route'],'version_impacts')
        roles=[m['role'] for m in snapshots[1][0]];self.assertLess(roles.index('tool'),len(roles)-1)
        self.assertEqual(snapshots[1][0][-1]['role'],'user');self.assertIn('公告影响回答边界',snapshots[1][0][-1]['content'])
        self.assertEqual(snapshots[1][0][0],{'role':'system','content':'stable-persona'})

    def test_no_images_are_delivered_when_user_declines(self):
        payload={'results':[{'title':'攻略','game_version':'documents-v1','source_refs':[{'source_id':'g','revision':1,'origin':'https://example.com'}],
                            'images':[{'image_id':'x','source_id':'g','source_revision':1}]}]}
        events,snapshots,execute=self.run_routed('角色甲配队，不要图',raw(['team'],['character:1']),'search_documents',{'query':'配队'},payload)
        tool_result=next(m for m in snapshots[1][0] if m['role']=='tool')
        self.assertNotIn('images',json.loads(tool_result['content'])['results'][0])
        self.assertFalse(any(s.get('images') for e in events if e['type']=='knowledge_trace' for s in e['sources']))

    def test_required_images_survive_model_omitting_image_keyword(self):
        events,snapshots,execute=self.run_routed('角色甲武器给我看看',raw(['build'],['character:1']),'search_documents',{'query':'武器'})
        self.assertIn('原图',json.loads(execute.call_args.args[1])['query'])

    def test_known_gender_record_is_sent_by_id_and_not_collapsed_to_shared_name(self):
        with closing(sqlite3.connect(self.kb)) as c,c:
            c.executescript("CREATE TABLE character_profiles(character_id,sex); INSERT INTO character_profiles VALUES(3,'男'),(5,'女');")
        choices=intent.candidates([{'role':'user','text':'女漂泊者导电的技能'}])
        self.assertEqual(next(e['sex'] for e in choices if e['key']=='character:5'),'女')
        plan=self.resolve('女漂泊者导电的技能',raw(['mechanism'],['character:5']))
        args=intent.scoped_arguments(plan,'search_wiki',{'entity_name':'漂泊者·导电','query':'技能'})
        self.assertEqual(args['entity_name'],'5')
        with self.assertRaises(ValueError): intent.scoped_arguments(plan,'search_wiki',{'entity_name':'武器甲','entity_type':'weapon','query':'技能'})

    def test_oversized_current_question_is_not_sent_partially_to_parser(self):
        with patch.object(intent,'extract') as network:
            plan=intent.resolve_intent([{'role':'user','content':'字'*15000}])
        network.assert_not_called()
        self.assertEqual(plan['status'],'fallback')

    def test_chat_service_enables_routing_and_does_not_append_declined_images(self):
        events=[{'type':'intent_trace','trace':{'image_mode':'none'}},
                {'type':'knowledge_trace','sources':[{'images':[{'image_id':'x'}]}]}, {'type':'delta','content':'纯文字回答'}]
        history=[NS(id=1,role='user',content='不用图'),NS(id=2,role='user',content='武器呢')]
        with patch.object(chat_service,'run_agent',return_value=iter(events)) as agent, \
             patch.object(chat_service,'build_system_prompt',return_value='stable'), \
             patch.object(chat_service.context_service,'prepare_context',return_value=[]), \
             patch.object(chat_service.session_service,'list_messages',return_value=history), \
             patch.object(chat_service.session_service,'append_message',return_value=NS(id=3)) as append, \
             patch.object(chat_service,'_image_suffix') as images:
            output=[json.loads(e) for e in chat_service.stream_reply(NS(rollback=lambda:None),NS(id='s',owner_id=1),NS(),'武器呢')]
        self.assertTrue(agent.call_args.kwargs['intent_routing'])
        images.assert_not_called()
        self.assertEqual(append.call_args.args[3],'纯文字回答')
        self.assertTrue(any(e['type']=='done' for e in output))

    def test_model_cannot_invent_unrequested_filters_and_implicit_wiki_version_does_not_hide_guides(self):
        plan=self.resolve('角色甲机制和配队',raw(['mechanism','team'],['character:1']))
        args=intent.scoped_arguments(plan,'search_documents',{'query':'配队','game_version':'3.7','author':'某作者','updated_on':'2026-07-09','as_of':'2026-07-09'})
        self.assertFalse({'game_version','author','updated_on','as_of'} & args.keys())
        batches=[('search_wiki',{'query':'机制','entity_name':'角色甲'}),('search_documents',{'query':'配队','entity_name':'角色甲'})]
        snapshots=[]
        def stream(messages,**kw):
            snapshots.append(list(messages))
            if len(snapshots)<=2:
                name,args=batches[len(snapshots)-1]
                return iter([{'type':'tool_calls','tool_calls':[{'id':str(len(snapshots)),'function':{'name':name,'arguments':json.dumps(args)}}]}, {'type':'completion','finish_reason':'tool_calls'}])
            return iter([{'type':'delta','content':'基于不同来源分别说明。'},{'type':'completion','finish_reason':'stop'}])
        with patch.object(intent,'extract',return_value=raw(['mechanism','team'],['character:1'])), \
             patch.object(orchestrator,'stream_completion',side_effect=stream), \
             patch.object(orchestrator,'execute_tool',return_value=json.dumps({'results':[],'game_version':'3.7'})) as execute:
            list(orchestrator.run_agent([{'role':'user','content':'角色甲机制和配队'}],tools=tools.TOOLS,intent_routing=True))
        self.assertNotIn('game_version',json.loads(execute.call_args_list[1].args[1]))

    def test_knowledge_reply_correction_replaces_stream_and_persists_same_complete_text(self):
        events=[{'type':'knowledge_trace','sources':[]},{'type':'delta','content':'暂无3.8资料。等我资料齐了，第一个告诉你。'}]
        history=[NS(id=1,role='user',content='之前的问题'),NS(id=2,role='user',content='3.8呢')]
        with patch.object(chat_service,'run_agent',return_value=iter(events)), \
             patch.object(chat_service,'build_system_prompt',return_value='stable'), \
             patch.object(chat_service.context_service,'prepare_context',return_value=[]), \
             patch.object(chat_service.session_service,'list_messages',return_value=history), \
             patch.object(chat_service.session_service,'append_message',return_value=NS(id=3)) as append:
            output=[json.loads(e) for e in chat_service.stream_reply(NS(rollback=lambda:None),NS(id='s',owner_id=1),NS(),'3.8呢')]
        self.assertEqual(append.call_args.args[3],'暂无3.8资料。')
        self.assertEqual(next(e['content'] for e in output if e['type']=='replace'),'暂无3.8资料。')
        self.assertEqual(next(e['status'] for e in output if e['type']=='done'),'complete')


if __name__=='__main__': unittest.main()
