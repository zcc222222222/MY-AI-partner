"""Presentation links, separate from the immutable acquisition provenance."""
from urllib.parse import parse_qs, urlencode, urlsplit


def readable_source_url(value):
    if not isinstance(value, str) or any(ord(c) < 32 for c in value): return None
    try:
        p = urlsplit(value)
        if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password: return None
        if p.hostname == 'guide-server.aki-game.com':
            if p.scheme != 'https' or p.port or p.path != '/introduction/info': return None
            query = parse_qs(p.query)
            role, guide = query.get('roleGbId', []), query.get('id', [])
            if len(role) != 1 or len(guide) != 1 or not role[0].isascii() or not guide[0].isascii(): return None
            if not role[0].isdigit() or not guide[0].isdigit(): return None
            return 'https://mcguide.kurogames.com/?' + urlencode({'role_id':role[0], 'strategy_id':guide[0]})
        return value
    except ValueError:
        return None
