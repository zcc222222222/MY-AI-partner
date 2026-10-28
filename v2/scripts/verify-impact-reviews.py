"""Real-answer probes for scoped review results in impact and ordinary retrieval."""
import argparse
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from app.agents import orchestrator,tools
from app.agents.roleplay import build_system_prompt
from app.services.chat_service import _with_budget_fallback,_grounded_text,_source_suffix
from llmwiki import WikiStore,impact_reviews
from llmwiki.tools import WIKI_PATH

CASES=[
 {'id':'sample-reviewed-pending','tool':'get_version_impact','state':'insufficient_evidence',
  'question':'只按3.7公告复核记录，穗穗翾舞和栖霞饮露现在是尚未核对，还是已经核对但证据不足？候选武器名单这一条和20%加成推荐是否能一起称已验证3.7适用？简短回答，不要图。'},
 {'id':'qingxiao-scoped-no-change','tool':'get_version_impact','state':'no_text_change',
  'question':'查3.7清宵相关公告的复核结果。旧机制页与旧攻略现在是否仍未核对？所列结论无需文本修改是否代表整篇攻略已验证适用3.7？简短回答，不要图。'},
 {'id':'ordinary-guide-warning','tool':'search_documents','state':'insufficient_evidence',
  'question':'请先查询已收录的穗穗角色攻略，按现有来源说推荐武器，不限定游戏版本，并说明与该武器有关的公告复核状态。别将作者当时观点说成已验证当前最佳，不要图。'},
 {'id':'ordinary-mechanics-warning','tool':'search_wiki','state':'insufficient_evidence',
  'question':'只查3.7穗穗的机制Wiki说明翾舞触发，并带上对应公告复核状态，不把未验证的间隔补成具体秒数。不要图。'},
 {'id':'echo-partial-review','tool':'get_version_impact','state':'insufficient_evidence',
  'question':'查3.7千傀重楼效果修复关联的攻略复核结果：名称候选和暴击伤害词条推荐，是不是都已经验证无需修改？区分已核对和实战验证，不要图。'}
]


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--live',action='store_true');ap.add_argument('--case',action='append');ap.add_argument('--output',required=True);args=ap.parse_args()
    if not args.live:ap.error('--live is required for real-answer probes; use review-impacts.py for the offline queue')
    out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True)
    report={'schema':'impact-review-answer-acceptance-v1','queue':impact_reviews.queue(WikiStore(WIKI_PATH,create=False)),
            'cases':[],'limits':'自动校验真实工具、交付状态及预算；最终措辞仍须逐句对照，不能用关键词通过代替事实审核。'}
    card=json.loads((Path(__file__).resolve().parents[1]/'backend/characters/amis.json').read_text(encoding='utf-8'));system=build_system_prompt(SimpleNamespace(**card))
    for case in CASES:
        if args.case and case['id'] not in args.case:continue
        item={'id':case['id'],'question':case['question'],'answer':'','calls':[],'intent':None,'errors':[]};sources=[];original=orchestrator.execute_tool
        def execute(name,args):
            result=original(name,args);item['calls'].append({'name':name,'arguments':json.loads(args),'result':json.loads(result)});return result
        try:
            with patch.object(orchestrator,'execute_tool',side_effect=execute):
                for e in _with_budget_fallback(orchestrator.run_agent([{'role':'system','content':system},{'role':'user','content':case['question']}],tools=tools.TOOLS,intent_routing=True)):
                    if e['type']=='delta':item['answer']+=e['content']
                    elif e['type']=='replace':item['answer']=e['content'];item['errors'].append('generation_fallback')
                    elif e['type']=='intent_trace':item['intent']=e['trace']
                    elif e['type']=='knowledge_trace':sources.extend(e['sources'])
            item['answer']=_grounded_text(item['answer']);item['answer']+=_source_suffix(sources,item['answer'])
            calls=[c for c in item['calls'] if c['name']==case['tool']]
            if not calls:item['errors'].append('missing_expected_tool')
            states=[]
            for c in calls:
                if len(json.dumps(c['result'],ensure_ascii=False))>8000:item['errors'].append('budget')
                for page in c['result'].get('results',[]):
                    if case['tool']=='get_version_impact':states += [p['status'] for p in page['related_pages']]
                    else:states += [r['status'] for r in page.get('impact_reviews',[])]
            if case['state'] not in states:item['errors'].append('missing_review_state')
            if not item['answer'].strip():item['errors'].append('empty_answer')
            if item['intent'] and item['intent']['image_mode']!='none':item['errors'].append('image_preference')
        except Exception as exc:item['errors'].append(type(exc).__name__)
        item['passed']=not item['errors'];report['cases'].append(item);out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'id':item['id'],'passed':item['passed'],'errors':item['errors']},ensure_ascii=False),flush=True)
    report['passed']=bool(report['cases']) and all(c['passed'] for c in report['cases']);out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if report['passed'] else 1


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');raise SystemExit(main())
