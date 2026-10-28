"""Original-image evidence, no-link presentation and streamed/persisted reply parity."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS
from unittest.mock import patch
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.agents.guide_content import content_fields, image_url
from app.agents import intent
from app.services import chat_service as chat

POST = '1559169379056631808'
URL = 'https://www.kurobbs.com/mc/post/' + POST
BASE = 'https://prod-alicdn-community.kurobbs.com/forum/'


class LiveGuideImageTests(unittest.TestCase):
    def source(self):
        return {'source_kind': 'live_kurobbs', 'presentation': 'article', 'post_id': POST,
                'url': URL, 'title': '深塔攻略', 'author': '攻略作者', 'date_label': '帖子日期 2026-10-12',
                'images': [{'url': BASE + f'original{i}.png', 'post_id': POST, 'ordinal': i+1} for i in range(6)]}

    def test_only_original_post_images_allowed_and_text_marked_partial(self):
        for value in [BASE + 'a.svg', BASE + 'a.png?x=1', BASE + '../a.png',
                      BASE.replace('/forum/', '/avatar/') + 'a.png', BASE + 'a.png\n',
                      BASE.replace('kurobbs.com', 'kurobbs.com.evil.test') + 'a.png']:
            self.assertIsNone(image_url(value))
        fields = content_fields({'postId': POST, 'postContent': '<script>bad()</script>原文<em>预览</em>...',
            'imgCount': 1, 'imgContent': [{'url': BASE+'original.png'}, {'url': BASE+'original.png'}]})
        self.assertEqual(len(fields['images']), 1)
        self.assertNotIn('bad()', fields['text_excerpt'])
        self.assertEqual(fields['text_scope'], 'search_preview_not_full_article')

    def test_all_six_images_keep_author_order_and_reject_other_post(self):
        source = self.source()
        source['images'].append({'url': BASE+'wrong.png', 'post_id': 'other'})
        rendered = chat._image_suffix([source], '')
        self.assertEqual(rendered.count('!['), 6)
        self.assertNotIn('wrong.png', rendered)
        self.assertLess(rendered.index('original0.png'), rendered.index('original5.png'))
        credit = chat._source_suffix([source], '')
        self.assertIn('攻略作者', credit)
        self.assertNotIn('https://', credit)

    def test_graphic_request_restores_images_after_old_text_only_preference(self):
        turns = [{'role': 'user', 'text': '不用图片'}, {'role': 'user', 'text': '直接返回图文，不返回链接'}]
        self.assertEqual(intent.image_mode(turns), 'required')

    def test_final_reply_and_saved_history_have_six_images_and_no_post_hyperlink(self):
        events = [{'type': 'intent_trace', 'trace': {'image_mode': 'required'}},
                  {'type': 'knowledge_trace', 'sources': [self.source()]},
                  {'type': 'delta', 'content': f'当期攻略摘要。\n\n[查看原帖]({URL})\n链接：{URL}'}]
        with patch.object(chat, 'run_agent', return_value=iter(events)), \
             patch.object(chat, 'build_system_prompt', return_value='persona'), \
             patch.object(chat.context_service, 'prepare_context', return_value=[]), \
             patch.object(chat.session_service, 'list_messages', return_value=[NS(id=1, role='user', content='本期深塔攻略'),NS(id=2,role='user',content='图文')]), \
             patch.object(chat.session_service, 'append_message', return_value=NS(id=3)) as append:
            events = [json.loads(x) for x in chat.stream_reply(NS(rollback=lambda: None), NS(id='s',owner_id=1), NS(), '图文')]
        saved = append.call_args.args[3]
        self.assertNotIn(URL, saved)
        self.assertEqual(saved.count('!['), 6)
        streamed = ''
        for e in events:
            if e['type'] == 'delta': streamed += e['content']
            elif e['type'] == 'replace': streamed = e['content']
        self.assertEqual(streamed, saved)


if __name__ == '__main__': unittest.main()
