"""Live dated-document player probes; never starts the app or writes user chats."""
import argparse
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from dotenv import load_dotenv
load_dotenv(ROOT / 'backend/.env')
from app.agents import orchestrator, tools
from app.agents.roleplay import build_system_prompt
from app.services.chat_service import _source_suffix, _image_suffix

CASES = [
    ('guide_images', '只依据目前收录的爱弥斯作者攻略，简短说推荐的武器，并附上原站素材图片和来源。素材图标与攻略长图请分清。'),
    ('guide_sanhua', '只依据知识库现有的散华作者攻略，简短说她适合什么队伍、操作要点是什么，并说明作者、平台更新时间和适用版本是否明确。'),
    ('guide_baizhi', '只依据知识库现有的白芷作者攻略，简短说她在队里的作用和治疗手法，并说明作者、平台更新时间和适用版本是否明确。'),
    ('guide_lingyang', '只依据知识库现有的凌阳作者攻略，简短说推荐配队和输出操作要点，并说明作者、平台更新时间和适用版本是否明确。'),
    ('rover_electric', '只依据巡游天国FM平台更新于2026年9月30日的漂泊者·导电攻略，简短说配队建议和资料时效，不要混用气动漂泊者。'),
    ('rover_spectro', '只依据结星&丸子平台更新于2026年7月29日的漂泊者·衍射攻略，简短说推荐配队和资料时效，不要混用湮灭漂泊者。'),
    ('rover_havoc', '只依据结星&丸子平台更新于2026年7月29日的漂泊者·湮灭攻略，简短说推荐配队和资料时效，不要混用衍射漂泊者。'),
    ('guide', '根据巡游天国FM的平台更新于2026年7月9日的爱弥斯攻略，震谐队有哪些队友选择？这是作者建议还是官方最优配队？简短回答。'),
    ('rover', '气动漂泊者现有作者攻略是哪一天更新的？不能确定当前版本适用性就直说，不要混用其他元素的漂泊者。'),
    ('event', '以2026年10月17日为准，天工寻物结束了吗？无音消除已经开始了吗？分别说日期。'),
    ('endgame', '查询2026年10月17日的逆境深塔，回音之塔的深境干扰是什么？也说一下这期起止时间。'),
    ('official', '官方3.7版本说明里，心是什么属性和武器类型？新角色之外还新增了哪些声骸合鸣效果？简短列名称即可。'),
    ('missing_version', '请只依据3.8版本资料说爱弥斯当前最优配队，没有这个版本的资料就直说，别用旧攻略补充。'),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--live', action='store_true')
    ap.add_argument('--output', required=True)
    ap.add_argument('--case', action='append')
    args = ap.parse_args()
    if not args.live:
        ap.error('Use --live to call the configured model')
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    card = json.loads((ROOT / 'backend/characters/amis.json').read_text(encoding='utf-8'))
    system = build_system_prompt(SimpleNamespace(**card))
    report = {'system_sha256': hashlib.sha256(system.encode()).hexdigest(), 'cases': []}
    original = tools.execute_tool
    for case_id, query in CASES:
        if args.case and case_id not in args.case:
            continue
        item = {'id': case_id, 'query': query, 'answer': '', 'calls': [], 'traces': [], 'usage': []}
        report['cases'].append(item)
        sources = []
        def execute(name, arguments):
            raw = original(name, arguments)
            parsed = json.loads(raw)
            item['calls'].append({'name': name, 'arguments': json.loads(arguments or '{}'), 'status': parsed.get('status'), 'error': parsed.get('error')})
            return raw
        try:
            with patch.object(orchestrator, 'execute_tool', side_effect=execute):
                for event in orchestrator.run_agent([{'role': 'system', 'content': system}, {'role': 'user', 'content': query}], tools=tools.TOOLS):
                    if event['type'] == 'delta':
                        item['answer'] += event['content']
                    elif event['type'] == 'knowledge_trace':
                        item['traces'].append(event['trace'])
                        sources.extend(event['sources'])
                    elif event['type'] == 'usage':
                        item['usage'].append(event['usage'])
            item['answer'] += _source_suffix(sources, item['answer'])
            item['answer'] += _image_suffix(sources, item['answer'])
            item['status'] = 'complete'
        except Exception as exc:
            item['status'] = 'failed'
            item['error'] = type(exc).__name__
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'id': case_id, 'status': item['status'], 'calls': item['calls']}, ensure_ascii=False), flush=True)
    return int(any(c['status'] != 'complete' for c in report['cases']))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
