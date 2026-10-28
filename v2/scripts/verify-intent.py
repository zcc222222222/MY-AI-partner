"""Opt-in real-model intent acceptance; reads shared KB, never writes user chats."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from app.agents.intent import resolve_intent
from app.agents.orchestrator import run_agent
from app.agents.tools import TOOLS


def user(text): return {'role':'user','content':text}
def assistant(text): return {'role':'assistant','content':text}

CASES=[
    ('weapon',[user('爱弥斯推荐什么武器？')],{'topics':['build'],'entity':'爱弥斯','image_mode':'auto'}),
    ('profile',[user('爱弥斯的声优是谁？')],{'topics':['profile'],'entity':'爱弥斯'}),
    ('mechanism',[user('爱弥斯的共鸣技能触发条件是什么？')],{'topics':['mechanism'],'entity':'爱弥斯'}),
    ('followup',[user('爱弥斯怎么配队？'),assistant('可以再看她的装备。'),user('那她武器呢？')],{'topics':['build'],'entity':'爱弥斯'}),
    ('weapon_pronoun',[user('永远的启明星是什么武器？'),assistant('我们正在讨论永远的启明星。'),user('这把武器的属性呢？')],{'topics':['equipment','stats'],'entity':'永远的启明星'}),
    ('rover_correction',[user('气动漂泊者怎么养成？'),assistant('你还可以指定形态。'),user('说错了，改成导电漂泊者的武器推荐。')],{'topics':['build'],'entity':'漂泊者·导电'}),
    ('rover_ambiguous',[user('漂泊者怎么配队？')],{'ambiguous':True}),
    ('persona',[user('我是漂泊者，今天想跟你聊聊天。')],{'topics':['chat'],'no_entities':True}),
    ('dated_author',[user('只依据FireMage丶平台更新于2026年7月9日的爱弥斯攻略，说推荐的武器。')],{'topics':['build'],'author':'FireMage丶','updated_on':'2026-07-09','game_version':None}),
    ('version',[user('爱弥斯只依据3.8版本资料说配队，不能用旧版补充。')],{'topics':['team'],'game_version':'3.8'}),
    ('event_time',[user('以2026年10月17日为准，天工寻物结束了吗？')],{'topics':['event'],'as_of':'2026-10-17','updated_on':None}),
    ('no_images',[user('爱弥斯怎么养成？'),user('后面不用图，只要文字。'),assistant('好的。'),user('那武器呢？')],{'topics':['build'],'image_mode':'none','entity':'爱弥斯'}),
    ('show_followup',[user('爱弥斯推荐什么武器？'),assistant('我们在讨论爱弥斯的推荐武器。'),user('给我看看。')],{'topics':['build'],'image_mode':'required','entity':'爱弥斯'}),
    ('mixed',[user('爱弥斯的共鸣技能触发条件是什么，怎么配队？')],{'topics_all':['mechanism','team'],'entity':'爱弥斯'}),
    ('date_question',[user('爱弥斯推荐什么武器？'),assistant('来源FireMage丶的爱弥斯攻略，平台更新时间2026-07-09。'),user('刚才的建议是哪天更新的？')],{'entity':'爱弥斯','author':None,'as_of':None,'updated_on':None}),
]


def check(plan,expected):
    errors=[]
    if plan is None or plan.get('status')!='parsed': return ['intent_not_parsed']
    for key,value in expected.items():
        if key=='topics': ok=bool(set(value)&set(plan['topics']))
        elif key=='topics_all': ok=set(value)<=set(plan['topics'])
        elif key=='entity': ok=value in {e['name'] for e in plan['entities']}
        elif key=='no_entities': ok=not plan['entities']
        else: ok=plan.get(key)==value
        if not ok: errors.append(key)
    return errors


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--live',action='store_true')
    ap.add_argument('--output',required=True)
    ap.add_argument('--chat',action='store_true',help='Also run five actual routed answer turns')
    ap.add_argument('--chat-only',action='store_true',help='Only run the five routed answer turns')
    args=ap.parse_args()
    if not args.live: ap.error('--live is required to call the configured model')
    out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True)
    report={'cases':[],'chats':[]}
    def save(): out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    for name,messages,expected in ([] if args.chat_only else CASES):
        start=time.monotonic()
        plan=resolve_intent(messages)
        errors=check(plan,expected)
        report['cases'].append({'id':name,'messages':messages,'plan':plan,'errors':errors,'seconds':round(time.monotonic()-start,3)})
        save()
        print(json.dumps({'id':name,'passed':not errors,'errors':errors},ensure_ascii=False),flush=True)
    if args.chat or args.chat_only:
        history=[]
        for query in ('爱弥斯推荐什么武器？简短回答并说明来源。','后面不要图，刚才的武器建议是什么日期的？','漂泊者怎么配队？我还没确定元素。',
                      '现在说爱弥斯，只依据3.8版本资料说配队，没有对应资料就直说。',
                      '现在取消版本限制，只按现有来源，简短分别说爱弥斯的共鸣技能触发条件和推荐配队。'):
            history.append(user(query));item={'query':query,'answer':'','intent':None,'retrieval':[],'images':0,'tools':[]}
            for event in run_agent([{'role':'system','content':'你是鸣潮知识助手，基于实际检索证据回答，简洁，不编造。'},*history],tools=TOOLS,intent_routing=True):
                if event['type']=='delta':item['answer']+=event['content']
                elif event['type']=='intent_trace':item['intent']=event['trace']
                elif event['type']=='tool_metrics':item['tools'].append(event['name'])
                elif event['type']=='knowledge_trace':
                    item['retrieval'].append(event['trace'])
                    item['images']+=sum(len(s.get('images',[])) for s in event['sources'])
            history.append(assistant(item['answer']));report['chats'].append(item);save()
        chats=report['chats']
        report['chat_checks']={
            'all_parsed_complete':all(c['answer'].strip() and c['intent'] and c['intent']['status']=='parsed' for c in chats),
            'first_has_images':chats[0]['images']>0,
            'declined_images':chats[1]['intent']['image_mode']=='none' and chats[1]['images']==0,
            'rover_clarifies_without_retrieval':chats[2]['intent']['ambiguous'] and not chats[2]['retrieval'],
            'missing_version_kept':chats[3]['intent']['game_version']=='3.8' and any(r.get('game_version')=='3.8' and r.get('status')=='no_reviewed_match' for r in chats[3]['retrieval']),
            'mixed_routes_without_forced_patch':chats[4]['intent']['game_version'] is None and 'search_documents' in chats[4]['tools'] and bool({'search_wiki','get_character_skills'}&set(chats[4]['tools'])),
        }
    report['passed']=all(not c['errors'] for c in report['cases']) and all(report.get('chat_checks',{}).values());save()
    return 0 if report['passed'] else 1


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
