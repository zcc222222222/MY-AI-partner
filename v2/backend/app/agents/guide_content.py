"""Validated, transient original image references from a specific public Kuro post."""
import re
from html import unescape
from urllib.parse import urlsplit


def image_url(value):
    if not isinstance(value, str) or len(value) > 500 or re.search(r'[\x00-\x20\\%]', value): return None
    try:
        p = urlsplit(value)
        if (p.scheme != 'https' or p.netloc != 'prod-alicdn-community.kurobbs.com'
                or p.query or p.fragment or '..' in p.path
                or not re.fullmatch(r'/forum/[A-Za-z0-9/_-]+\.(?:png|jpe?g|webp)', p.path, re.I)):
            return None
    except ValueError:
        return None
    return value


def content_fields(post):
    post_id = str(post.get('postId', ''))
    images, seen = [], set()
    for entry in (post.get('imgContent') or [])[:8]:
        if not isinstance(entry, dict): continue
        url = image_url(entry.get('url'))
        if not url or url in seen: continue
        seen.add(url)
        images.append({'url': url, 'post_id': post_id, 'ordinal': len(images)+1})
    text = str(post.get('postContent') or '')
    text = re.sub(r'<(?:script|style)\b[^>]*>.*?</(?:script|style)>', '', text, flags=re.S | re.I)
    text = ' '.join(unescape(re.sub(r'<[^>]*>', '', text)).split())[:800]
    return {'images': images, 'text_excerpt': text, 'text_scope': 'search_preview_not_full_article',
            'post_id': post_id, 'images_total': post.get('imgCount'),
            'images_partial': isinstance(post.get('imgCount'), int) and post['imgCount'] > len(images)}
