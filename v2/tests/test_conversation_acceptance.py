"""Reject misleading acceptance: persistence, real scope checks and actual evidence matter."""
from copy import deepcopy
from importlib import import_module
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
check_turn=import_module('verify-conversations').check_turn
recheck_report=import_module('verify-conversations').recheck_report


class ConversationAcceptanceTests(unittest.TestCase):
    def fixture(self):
        case={'entity':'character:1','image_mode':'none','game_version':None,'required_source':'a','allowed_sources':['a']}
        item={'intent':{'status':'parsed','ambiguous':False,'entity_keys':['character:1'],'image_mode':'none','game_version':None},
              'complete':True,'answer':'已核对，无法确认零链。','stored_answer':'已核对，无法确认零链。',
              'calls':[{'name':'search_documents','arguments':{'query':'零链完整循环'},'result':{'status':'found','results':[{'source_refs':[{'source_id':'a'}],'guide_context':{'rotation_segments':[]}}]}}]}
        return case,item

    def test_checks_persistence_and_actual_calls_not_just_plan(self):
        case,item=self.fixture();self.assertEqual(check_turn(case,item),[])
        item['stored_answer']='半截';item['calls'][0]['arguments']['game_version']='3.8'
        self.assertIn('stream_storage_mismatch',check_turn(case,item));self.assertIn('tool_version_scope',check_turn(case,item))

    def test_unknown_chain_can_be_inspected_but_requires_explicit_scope_check(self):
        case,item=self.fixture();case['uncovered_chain']=True
        broad=deepcopy(item['calls'][0]);broad['arguments']['query']='循环轴';broad['result']['results'][0]['guide_context']['rotation_segments']=[{'chain_scope':'原文未标明链数'}]
        item['calls'].append(broad);self.assertEqual(check_turn(case,item),[])
        item['calls']=item['calls'][1:];self.assertIn('no_explicit_chain_check',check_turn(case,item))

    def test_rejects_wrong_source_and_unverified_chain_actions(self):
        case,item=self.fixture();case['uncovered_chain']=True
        result=item['calls'][0]['result']['results'][0];result['source_refs'][0]['source_id']='other'
        result['guide_context']['rotation_segments']=[{'chain_scope':'原文未标明链数'}]
        errors=check_turn(case,item);self.assertIn('wrong_source',errors);self.assertIn('unverified_chain_flow_delivered',errors)

    def test_empty_answer_claim_requires_actual_no_match_and_no_images(self):
        case,item=self.fixture();case['empty']=True
        self.assertIn('expected_no_match',check_turn(case,item))
        item['calls']=[];self.assertIn('no_document_call',check_turn(case,item))
        item['answer']='![x](/image)';self.assertIn('declined_images',check_turn(case,item))

    def test_source_reuse_requires_actual_prior_delivery_and_expected_answer(self):
        case,item=self.fixture();case.update(allow_source_reuse=True,answer_need=[['2026-07-09']]);item['calls']=[]
        self.assertIn('no_document_call',check_turn(case,item))
        item['prior_sources']=['other'];self.assertIn('missing_required_source',check_turn(case,item))
        item['prior_sources']=['a'];self.assertTrue(any(e.startswith('answer_term') for e in check_turn(case,item)))
        item['answer']=item['stored_answer']='来源日期为2026-07-09。'
        self.assertEqual(check_turn(case,item),[])

    def test_fallback_requires_verified_tool_entity_and_recheck_preserves_failures(self):
        case,item=self.fixture();item['intent'].update(status='fallback',entity_keys=[])
        self.assertIn('entity_scope',check_turn(case,item))
        item['calls'][0]['result']['trace']={'entity':'character:1'}
        self.assertEqual(check_turn(case,item),[])
        item.update(question='问题',errors=['intent_not_parsed'],passed=False)
        suite={'scenarios':[{'id':'s','turns':[{**case,'question':'问题'}]}]}
        old={'scenarios':[{'id':'s','turns':[item]}]};new=recheck_report(old,suite)
        self.assertTrue(new['passed']);self.assertEqual(new['scenarios'][0]['turns'][0]['previous_errors'],['intent_not_parsed'])
        self.assertFalse(old['scenarios'][0]['turns'][0]['passed'])
        old['scenarios'][0]['turns'][0]['question']='换掉问题'
        with self.assertRaises(ValueError):recheck_report(old,suite)


if __name__=='__main__':unittest.main()
