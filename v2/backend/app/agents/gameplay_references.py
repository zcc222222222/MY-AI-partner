"""User-supplied guide references; no Weibo crawl, cookies, image copy or OCR."""
from datetime import date, datetime
import json
from pathlib import Path
import re

from .guide_periods import LOCAL

PATH = Path(__file__).with_name('gameplay_references.json')
POLICY = (
    '这是人工收录的微博原帖入口，作者及适用日期来自用户提供的链接和截图，不是本次联网抓取结果。'
    '可以展示作者、标题、作者标注的适用日期和原帖链接。必须说明正文尚未读取；不能生成图中的配队、信物或操作结论。'
    '作者标注周期不等于官方周期已核实；不能声称已自动追踪该作者或取得完整图文。'
    '此来源按links展示，保留微博原帖入口，不复制作者标注禁止未授权转载的导图。'
)


def lookup(query, *, game_version=None, author=None, updated_on=None, as_of=None):
    if (not isinstance(query, str) or not 1 <= len(query.strip()) <= 500
            or not re.search(r'冥歌海墟|海墟', query)
            or re.search(r'深塔|矩阵|上期|上一期|往期|历史|旧攻略', query)
            or re.search(r'(?:不要|不看|不查|排除|不用|别).{0,8}微博|只(?:查询|查|看|用)?库街区', query)
            or ('库街区' in query and '微博' not in query)
            or updated_on is not None):
        return None
    # Exact historical period labels must not silently resolve to today's link.
    from .live_guides import period_labels
    requested = period_labels(query)
    moment = date.fromisoformat(as_of) if as_of else datetime.now(LOCAL).date()
    try:
        if PATH.stat().st_size > 100_000:
            return None
        data = json.loads(PATH.read_text(encoding='utf-8'))
        if data.get('schema') != 'provided-gameplay-references-v1':
            return None
        for entry in data['entries']:
            if entry.get('platform') != 'weibo' or entry.get('topic') != '冥歌海墟':
                continue
            uid, post = entry['account_id'], entry['post_id']
            if not re.fullmatch(r'\d{5,20}', uid) or not re.fullmatch(r'[A-Za-z0-9]{5,20}', post):
                continue
            url = f'https://weibo.com/{uid}/{post}'
            if entry['url'] != url or entry['body_read'] is not False or entry['automatic_refresh'] is not False:
                continue
            if author and author.lstrip('@') != entry['author']:
                continue
            if game_version and game_version not in entry['claimed_game_versions']:
                continue
            start, end = date.fromisoformat(entry['claimed_start_date']), date.fromisoformat(entry['claimed_end_date'])
            # The graphic supplies dates, not exact server-time boundaries.
            # On either boundary date, do not offer it as a default current hit.
            if not start < moment < end or requested and requested != [start.isoformat()]:
                continue
            # A link-only entry cannot satisfy other dates, floors or numeric
            # conditions that have not been checked against the unread post.
            remaining = query
            for version in entry['claimed_game_versions']:
                remaining = remaining.replace(version, '')
            date_pattern = (rf'(?:{start.year}[年./-])?0?{start.month}[月./-]0?{start.day}日?'
                            rf'|(?:{start:%Y%m%d}|{start:%y%m%d})期')
            remaining = re.sub(date_pattern, '', remaining)
            if re.search(r'\d', remaining):
                continue
            row = {'title': entry['title'], 'url': url, 'author': entry['author'], 'post_id': post,
                   'account_id': uid, 'topic': entry['topic'], 'source_kind': 'provided_weibo_reference',
                   'post_date': None, 'game_version': None,
                   'date_label': f'作者标注适用日期 {start.isoformat()}—{end.isoformat()}（精确边界未核实，正文未读取）',
                   'claimed_game_versions': entry['claimed_game_versions'], 'body_read': False,
                   'content_status': 'reference_only', 'currency_verified': False,
                   'evidence_basis': entry['evidence_basis'], 'images': []}
            return {'status': 'reference_available', 'results': [row], 'presentation': 'links',
                    'currency_verified': False, 'schedule_verified': False, 'body_read': False,
                    'automatic_refresh': False, 'note': POLICY}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return None
