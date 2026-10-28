"""Saved-announcement impact acceptance. --live uses the configured persona and actual tools."""
import argparse
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from llmwiki.tools import get_version_impact
from app.agents import orchestrator,tools
from app.agents.roleplay import build_system_prompt
from app.services.chat_service import _with_budget_fallback,_source_suffix,_grounded_text

CASES=[
 {'id':'suisui-related-weapon','version':'3.7','entity':'穗穗','query':'翾舞触发 修复 旧攻略',
  'ids':['suisui-timing','qixia-effect'],'review':True,
  'question':'按已审核的3.7公告，穗穗的翾舞触发修复以及相关武器修复，会不会让旧攻略直接失效？保留公告条件，说明哪些只是需要复核，不要推算增伤。简短回答，不要图。'},
 {'id':'qianxiao-visual','version':'3.7','entity':'千咲','query':'模型修复','ids':['qianxiao-model'],'review':False,
  'question':'只查3.7官方公告中千咲穿蜜桃冰服饰的修复，这是战斗数值削弱吗？按公告实际文字回答，不要图。'},
 {'id':'qingxiao-distinctions','version':'3.7','entity':'清宵','query':'问题修复','ids':['qingxiao-movement','qingxiao-model'],'review':True,
  'question':'按3.7官方更新，清宵修了哪些问题？区分显示表现和行动异常，旧机制页、攻略是否只是需要复核？不要把修复说成加伤，不要图。'},
 {'id':'missing-character','version':'3.7','entity':'爱弥斯','query':'公告调整','empty':True,
  'question':'只查3.7已审核公告影响清单，爱弥斯有明确调整记录吗？没有匹配就说明清单覆盖不足，不能说游戏里一定没调整。不要图。'},
 {'id':'missing-version','version':'3.8','entity':'穗穗','query':'公告修复','empty':True,
  'question':'只查3.8已审核公告影响记录，穗穗有修复吗？缺失就直说，不能拿3.7来补答。不要图。'},
 {'id':'tower-recommendation','version':'3.7','entity':None,'query':'逆境深塔 阵容推荐','ids':['tower-recommend'],'review':False,
  'question':'3.7公告说优化逆境深塔阵容推荐，是不是环境增益或怪物抗性变了？只依据已审核公告影响记录区分，不要猜新词条，不要图。'},
 {'id':'weapon-to-guide','version':'3.7','entity':'栖霞饮露','entity_type':'weapon','query':'效果修复 关联攻略','ids':['qixia-effect'],'review':True,
  'question':'只按3.7官方公告，栖霞饮露修复了什么？哪些已入库攻略提到它、需要复核？保留生效条件，不把关联说成攻略失效，不要图。'}
]


def check(case,results):
    errors=[];rows=[r for result in results for r in result.get('results',[])]
    if not results:errors.append('no_impact_call')
    if any(r.get('game_version')!=case['version'] for r in results):errors.append('version_scope')
    if case.get('empty'):
        if rows or any(r.get('status')!='no_reviewed_match' for r in results):errors.append('expected_no_match')
    else:
        if not set(case['ids'])<={r['id'] for r in rows}:errors.append('missing_changes')
        if case.get('review') is False and any(r['review_required'] for r in rows):errors.append('presentation_promoted_to_mechanics')
        if any(r['source_id']!='official/news/5569' or r['source_revision']!=2 for r in rows):errors.append('wrong_source')
        if any(p['status'] not in ('needs_review','no_text_change','insufficient_evidence','revision_required','revision_published') for r in rows for p in r['related_pages']):errors.append('false_invalidation')
    if any(len(json.dumps(r,ensure_ascii=False))>8000 for r in results):errors.append('budget')
    return errors


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--live',action='store_true');ap.add_argument('--case',action='append');ap.add_argument('--output',required=True);args=ap.parse_args()
    out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True)
    report={'schema':'notice-impact-acceptance-v1','cases':[],'limits':'自动校验来源、分类、关联状态和预算；逐句表述仍须人工核对。不会新增爬取或写用户会话。'}
    system=build_system_prompt(SimpleNamespace(**json.loads((Path(__file__).resolve().parents[1]/'backend/characters/amis.json').read_text(encoding='utf-8'))))
    for case in CASES:
        if args.case and case['id'] not in args.case:continue
        evidence=get_version_impact(case['version'],case['entity'],entity_type=case.get('entity_type','character'),query=case['query'],limit=8)
        item={'id':case['id'],'question':case['question'],'evidence':evidence,'errors':check(case,[evidence])}
        if args.live:
            item.update(answer='',calls=[],intent=None,traces=[]);sources=[];original=orchestrator.execute_tool
            def execute(name,arguments):
                result=original(name,arguments);item['calls'].append({'name':name,'arguments':json.loads(arguments),'result':json.loads(result)});return result
            try:
                with patch.object(orchestrator,'execute_tool',side_effect=execute):
                    for e in _with_budget_fallback(orchestrator.run_agent([{'role':'system','content':system},{'role':'user','content':case['question']}],tools=tools.TOOLS,intent_routing=True)):
                        if e['type']=='delta':item['answer']+=e['content']
                        elif e['type']=='replace':item['answer']=e['content'];item['errors'].append('generation_fallback')
                        elif e['type']=='intent_trace':item['intent']=e['trace']
                        elif e['type']=='knowledge_trace':item['traces'].append(e['trace']);sources.extend(e['sources'])
                item['answer']=_grounded_text(item['answer']);item['answer']+=_source_suffix(sources,item['answer'])
                docs=[c['result'] for c in item['calls'] if c['name']=='get_version_impact'];item['errors']+=check(case,docs)
                if not item['intent'] or item['intent']['game_version']!=case['version']:item['errors'].append('intent_version')
                if not item['answer'].strip():item['errors'].append('empty_answer')
            except Exception as exc:item['errors'].append(type(exc).__name__)
        item['passed']=not item['errors'];report['cases'].append(item)
        out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'id':case['id'],'passed':item['passed'],'errors':item['errors']},ensure_ascii=False),flush=True)
    report['passed']=bool(report['cases']) and all(c['passed'] for c in report['cases']);out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if report['passed'] else 1


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');raise SystemExit(main())
