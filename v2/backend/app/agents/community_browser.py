"""Reusable anonymous browser worker. Emits post metadata, never tokens or bodies."""
import json
import re
import sys
import time
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, getproxies
from urllib.robotparser import RobotFileParser

UA = 'MYAIPARTNER-GuideLinks/1.0'
SEARCH = 'https://api.kurobbs.com/forum/search/v2/join'
STAGE = 'initializing'


def retry_delay(value):
    try:
        delay = float(value)
        return max(600.0, delay) if math.isfinite(delay) else 600.0
    except (ValueError, TypeError):
        try:
            return max(600.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 600.0


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


def page_policy(keyword):
    opener = build_opener(NoRedirect())
    try:
        with opener.open(Request('https://www.kurobbs.com/robots.txt', headers={'User-Agent': UA}), timeout=8) as response:
            body = response.read(100001)
            if len(body) > 100000: raise ValueError('robots too large')
            text = body.decode('utf-8')
    except HTTPError as exc:
        if exc.code != 404: raise
        text = ''
    parser = RobotFileParser()
    parser.parse(text.splitlines())
    url = 'https://www.kurobbs.com/mc/search?q=' + quote(keyword)
    if not parser.can_fetch(UA, url): raise ValueError('robots disallowed')
    return url, max(6.0, float(parser.crawl_delay(UA) or 0))


class BrowserSession:
    def __init__(self):
        self.runtime = self.browser = self.context = self.page = None
        self.policy_cache = {}
        self.next_at = 0.0

    def close(self):
        for resource in (self.browser, self.runtime):
            if resource:
                try:
                    resource.close() if resource is self.browser else resource.stop()
                except Exception: pass
        self.runtime = self.browser = self.context = self.page = None

    def ensure_page(self):
        global STAGE
        if self.browser and self.browser.is_connected() and self.page and not self.page.is_closed():
            return self.page
        self.close()
        from playwright.sync_api import sync_playwright
        STAGE = 'launch'
        self.runtime = sync_playwright().start()
        options = {'headless': True, 'args': ['--autoplay-policy=user-gesture-required']}
        configured_proxy = getproxies().get('https') or getproxies().get('http')
        if configured_proxy:
            parsed = urlsplit(configured_proxy)
            options['proxy'] = {'server': f'{parsed.scheme}://{parsed.hostname}:{parsed.port or 80}'}
            if parsed.username:
                options['proxy'].update({'username': parsed.username, 'password': parsed.password or ''})
        self.browser = self.runtime.chromium.launch(**options)
        # Dedicated anonymous context; never the user's browser profile or login state.
        self.context = self.browser.new_context(locale='zh-CN', accept_downloads=False)
        self.page = self.context.new_page()
        return self.page

    def search(self, keyword):
        global STAGE
        STAGE = 'robots'
        policy = self.policy_cache.get(keyword)
        if not policy or time.monotonic() >= policy[0]:
            url, interval = page_policy(keyword)
            self.next_at = max(self.next_at, time.monotonic()+interval)
            self.policy_cache[keyword] = (time.monotonic()+600, url, interval)
            if len(self.policy_cache) > 64: self.policy_cache.pop(next(iter(self.policy_cache)))
        else: _, url, interval = policy
        if interval > 20: raise ValueError('crawl delay exceeds request budget')
        # Keep site-wide spacing across searches and retries, including sort requests.
        wait = self.next_at - time.monotonic()
        if wait > 0: time.sleep(wait)
        self.next_at = time.monotonic() + interval
        page = self.ensure_page()
        try:
            blocked = []
            search_observations = []
            last_search = [0.0]
            def is_search(response):
                return response.url.split('?', 1)[0] == SEARCH and response.request.method == 'POST'
            def inspect(response):
                if response.url.split('?',1)[0] == SEARCH:
                    search_observations.append({'method':response.request.method,'status':response.status})
                if (response.url.split('?', 1)[0] == SEARCH or response.request.is_navigation_request()) and response.status in (401, 403, 429):
                    blocked.append({'http_status': response.status,
                                    'retry_after': retry_delay(response.headers.get('retry-after'))})
            page.on('response', inspect)
            STAGE = 'navigation'
            page.goto('about:blank')
            with page.expect_response(is_search, timeout=35000) as first:
                page.goto(url, wait_until='commit', timeout=20000)
                STAGE = 'search_response'
            response = first.value
            last_search[0] = time.monotonic()
            self.next_at = last_search[0] + interval
            # The normal site UI exposes this sort; no invented API parameters.
            latest = False
            if response.status == 200 and not re.search(r' \d{6}$', keyword) and page.get_by_role('button', name='相关', exact=True).count():
                STAGE = 'sort'
                page.get_by_role('button', name='相关', exact=True).click(timeout=4000)
                wait = interval - (time.monotonic() - last_search[0])
                if wait > 0: time.sleep(wait)
                with page.expect_response(is_search, timeout=15000) as sorted_response:
                    page.get_by_role('menuitem', name='最新', exact=True).click(timeout=4000)
                response = sorted_response.value
                self.next_at = time.monotonic() + interval
                latest = True
            if blocked or response.status != 200:
                return {'status': 'access_restricted' if blocked or response.status in (401,403,429) else 'source_unavailable',
                        **(blocked[0] if blocked else {'http_status': response.status})}
            if len(response.body()) > 2000000:
                raise ValueError('Search response too large')
            payload = response.json()
            if payload.get('code') != 200:
                return {'status': 'source_unavailable'}
            posts = payload.get('data', {}).get('post')
            if not isinstance(posts, dict) or not isinstance(posts.get('postList'), list):
                raise ValueError('Search schema changed')
            challenge = page.get_by_text(re.compile(r'请完成验证|滑动验证|人机验证|请登录后搜索'))
            if challenge.count() and challenge.first.is_visible():
                return {'status': 'access_restricted'}
            raw = posts['postList']
            rows = [{k: r.get(k) for k in ('postId', 'postTitle', 'userName', 'gameId', 'gameForumId',
                     'postStatus', 'reviewStatus', 'createTimestamp', 'topicList', 'postContent',
                     'imgContent', 'imgCount')} for r in raw[:100]]
            # Public post text previews and original image metadata only, never video/cookie/token data.
            return {'status': 'ok', 'posts': rows, 'records_seen': len(raw),
                    'partial': bool(posts.get('hasNext')) or len(raw) > 100,
                    'sort': 'latest' if latest else 'relevance', 'body_read': False}
        except Exception as exc:
            self.close()
            if blocked:
                return {'status': 'access_restricted', **blocked[0]}
            failure = {'status':'source_unavailable','failure_stage':STAGE,'error_type':type(exc).__name__,
                       'search_observations':search_observations[:4]}
            code = re.search(r'net::(ERR_[A-Z_]+)', str(exc))
            if code: failure['network_error'] = code[1]
            return failure
        finally:
            try: page.remove_listener('response', inspect)
            except Exception: pass


def safe_search(session, keyword):
    global STAGE
    STAGE = 'initializing'
    try:
        if not isinstance(keyword, str) or not 1 <= len(keyword) <= 100:
            raise ValueError('Invalid keyword')
        return session.search(keyword)
    except HTTPError as exc:
        return {'status': 'access_restricted' if exc.code in (401, 403, 429) else 'source_unavailable',
                'retry_after': retry_delay(exc.headers.get('Retry-After'))}
    except Exception as exc:
        result = {'status': 'source_unavailable', 'failure_stage': STAGE, 'error_type': type(exc).__name__}
        if isinstance(exc, ValueError) and str(exc) == 'robots disallowed': result['failure_reason'] = 'robots_disallowed'
        code = re.search(r'net::(ERR_[A-Z_]+)', str(exc))
        if code: result['network_error'] = code[1]
        session.close()
        return result


def search(keyword):
    session = BrowserSession()
    try: return safe_search(session, keyword)
    finally: session.close()


if __name__ == '__main__':
    session = BrowserSession()
    try:
        if '--serve' in sys.argv:
            for line in sys.stdin:
                try: result = safe_search(session, json.loads(line)['keyword'])
                except (ValueError, KeyError, TypeError): result = {'status': 'source_unavailable', 'error_type': 'ValueError'}
                print(json.dumps(result, ensure_ascii=True), flush=True)
        else:
            print(json.dumps(safe_search(session, json.load(sys.stdin)['keyword']), ensure_ascii=True))
    finally: session.close()
