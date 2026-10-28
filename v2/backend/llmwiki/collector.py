"""Bounded public document collection; no login, browser impersonation or auto-publication."""
from contextlib import closing
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import time
from urllib.parse import quote, unquote, urljoin, urlsplit

UA = "MY-AIPARTNER-KnowledgeCollector/0.1"
HOSTS = {"mcguide.kurogames.com", "guide-server.aki-game.com", "wiki.biligame.com",
         "mc.kurogames.com", "media-cdn-mingchao.kurogame.com", "www.taptap.cn"}
DOCUMENT_SCOPE = "documents-v1"  # Corpus namespace, deliberately NOT a game patch.


class CollectionError(ValueError):
    def __init__(self, message, *, host=None, status=None, retry_at=None, retryable=False):
        super().__init__(message)
        self.host, self.status, self.retry_at, self.retryable = host, status, retry_at, retryable


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def robots_policy(text, path):
    """Select the longest matching UA group, then longest path; allow wins ties."""
    groups, agents, rules, seen_rule = [], [], [], False
    for line in text.splitlines() + ["User-agent: __end__"]:
        line = line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (p.strip() for p in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if seen_rule:
                groups.append((agents, rules))
                agents, rules, seen_rule = [], [], False
            agents.append(value.lower())
        elif agents and key in ("allow", "disallow", "crawl-delay"):
            seen_rule = True
            rules.append((key, value))
    ranked = [(max((len(a) if a != "*" else 0 for a in aa if a == "*" or a in UA.lower()), default=-1), rr)
              for aa, rr in groups]
    best = max((score for score, _ in ranked), default=-1)
    matches, delay = [], 0.0
    for score, rr in ranked:
        if score != best or score < 0:
            continue
        for key, value in rr:
            if key == "crawl-delay":
                try:
                    delay = max(delay, float(value))
                except ValueError:
                    pass
            elif value:
                pattern = re.escape(unquote(value)).replace(r"\*", ".*")
                if pattern.endswith(r"\$"):
                    pattern = pattern[:-2] + "$"
                if re.match(pattern, unquote(path)):
                    matches.append((len(value.replace("*", "").rstrip("$")), key == "allow"))
    return (max(matches)[1] if matches else True), delay


class Collector:
    def __init__(self, directory, interval=5, *, clock=time.time, sleep=time.sleep):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.interval = max(5.0, float(interval))
        self.clock, self.sleep = clock, sleep
        self.robots = {}
        self.log = []
        self.state = self.directory / "crawl-state.db"
        with closing(sqlite3.connect(self.state)) as conn, conn:
            conn.executescript("CREATE TABLE IF NOT EXISTS hosts(host TEXT PRIMARY KEY, last REAL, blocked REAL);"
                               "CREATE TABLE IF NOT EXISTS cache(url TEXT PRIMARY KEY, path TEXT, fetched REAL, headers TEXT);")

    def fetch(self, url, *, robots=False, refresh=False):
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in HOSTS or parts.username or parts.password or parts.port:
            raise CollectionError("URL is outside the public source allowlist")
        if not robots and parts.hostname not in self.robots:
            raw, _, status = self.fetch(f"https://{parts.hostname}/robots.txt", robots=True, refresh=refresh)
            if status == 200 and b"<html" not in raw.lower():
                self.robots[parts.hostname] = raw.decode("utf-8", errors="replace")
            elif status in (404, 410) or (300 <= status < 400):
                # No usable directives; preserve this fact, do not call it explicit permission.
                self.robots[parts.hostname] = ""
            else:
                raise CollectionError(f"Unusable robots response: {parts.hostname} HTTP {status}")
        allowed, delay = robots_policy(self.robots.get(parts.hostname, ""), parts.path + ("?" + parts.query if parts.query else ""))
        if not robots and not allowed:
            raise CollectionError("robots.txt disallows this URL")
        safe_url = quote(url, safe=":/?=&%+~;,@!$'()*-._")
        key = hashlib.sha256(url.encode()).hexdigest()
        # The transaction also serializes separate collector processes.
        with closing(sqlite3.connect(self.state, timeout=120)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            host = conn.execute("SELECT last,blocked FROM hosts WHERE host=?", (parts.hostname,)).fetchone()
            if host and host[1] > self.clock():
                raise CollectionError(f"Host paused until {host[1]} after a previous refusal", host=parts.hostname,
                                      retry_at=host[1], retryable=True)
            cached = conn.execute("SELECT path,fetched,headers FROM cache WHERE url=?", (url,)).fetchone()
            if cached and not refresh and self.clock() - cached[1] < 86400 and Path(cached[0]).is_file():
                self.log.append({"url": url, "cache": True, "fetched_at": cached[1]})
                return Path(cached[0]).read_bytes(), json.loads(cached[2]), 200
            if host:
                self.sleep(max(0, host[0] + max(self.interval, delay) - self.clock()))
            headers_path, body_path = self.directory / (key + ".headers"), self.directory / (key + ".tmp")
            started = self.clock()
            try:
                result = subprocess.run(["curl.exe", "-sS", "--connect-timeout", "10", "--max-time", "25",
                                         "--max-filesize", "10485760", "-A", UA, "-H", "x-language: zh-Hans",
                                         "-D", str(headers_path), "-o", str(body_path), "-w", "%{http_code}", safe_url],
                                        capture_output=True, timeout=30)
                status = int(result.stdout or b"0")
                raw = body_path.read_bytes() if body_path.exists() else b""
                headers = {}
                for line in headers_path.read_text(encoding="latin-1").splitlines() if headers_path.exists() else []:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        if k.lower() in ("content-type", "etag", "last-modified", "retry-after", "location"):
                            headers[k.lower()] = v.strip()
                blocked = 0
                if status in (401, 403, 429) or b"<title>captcha" in raw.lower():
                    blocked = self.clock() + 86400
                    if status == 429 and headers.get("retry-after"):
                        value = headers["retry-after"]
                        try:
                            wait = float(value)
                        except ValueError:
                            try:
                                wait = parsedate_to_datetime(value).timestamp() - self.clock()
                            except (ValueError, TypeError, OverflowError):
                                wait = 86400
                        blocked = self.clock() + max(60, wait)
                conn.execute("INSERT OR REPLACE INTO hosts VALUES (?,?,?)", (parts.hostname, self.clock(), blocked))
                self.log.append({"url": url, "status": status, "started_at": started, "finished_at": self.clock(),
                                 "bytes": len(raw), "robots": robots})
                # Keep refusal state even when this request raises.
                conn.commit()
                if result.returncode or blocked or (status != 200 and not robots):
                    raise CollectionError(f"Collection stopped for {parts.hostname}: HTTP {status}, curl={result.returncode}",
                                          host=parts.hostname, status=status, retry_at=blocked or None,
                                          retryable=status in (0, 408, 429) or status >= 500)
                if len(raw) > 10485760:
                    raise CollectionError("Document exceeds 10 MiB")
                if status == 200:
                    target = self.directory / (hashlib.sha256(raw).hexdigest() + ".raw")
                    target.write_bytes(raw)
                    conn.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?,?)", (url, str(target.resolve()), self.clock(), json.dumps(headers)))
                return raw, headers, status
            except subprocess.SubprocessError as exc:
                raise CollectionError(f"Network process failed for {parts.hostname}: {type(exc).__name__}",
                                      host=parts.hostname, retryable=True) from exc
            finally:
                # Network errors still consume the interval.
                conn.execute("INSERT INTO hosts VALUES (?,?,0) ON CONFLICT(host) DO UPDATE SET last=excluded.last",
                             (parts.hostname, self.clock()))
                conn.commit()
                body_path.unlink(missing_ok=True)

    def json(self, url):
        raw, _, _ = self.fetch(url)
        return json.loads(raw)


class HtmlText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.links, self.stack = [], [], []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        if tag == "img" and attrs.get("alt"):
            self.parts.append(" " + attrs["alt"] + " ")
        if tag in ("td", "th"):
            self.parts.append(" | ")
        if tag in ("p", "div", "li", "br", "tr", "h1", "h2", "h3", "h4", "section"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.skip = max(0, self.skip - 1)
        if tag in ("p", "li", "tr", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_data(self, text):
        if not self.skip:
            self.parts.append(text)

    @property
    def text(self):
        return "\n".join(line for part in "".join(self.parts).splitlines() if (line := re.sub(r"[ \t\xa0]+", " ", part).strip()))


def html_text(html):
    parser = HtmlText()
    parser.feed(html or "")
    return parser.text


def chinese(records):
    return next((item for item in records or [] if item.get("language") == "zh-Hans"), None)


def mcguide_document(listing, detail):
    base = chinese(detail.get("baseTexts"))
    if not base or str(detail.get("id")) != str(listing["id"]):
        raise CollectionError("Missing Chinese text or mismatching detail ID")
    modified = listing.get("modifiedAt")
    date = datetime.fromtimestamp(modified, timezone(timedelta(hours=8))).isoformat() if isinstance(modified, (int, float)) else None
    # Likes, collection counts, fetch timestamps and other readers' state are not document revisions.
    content = {k: v for k, v in detail.items() if k not in ("likeCount", "isLiked", "collectCount", "isCollected")}
    title = base.get("introductionName", "").strip()
    if not title:
        raise CollectionError("Empty Chinese guide title")
    return {"schema": "dated-source-v1", "title": title, "author": (base.get("introductionSource") or "").strip(),
            "source_date": date, "date_basis": "platform_modifiedAt", "game_version": None,
            "document_version": (date or "undated") + "/" + hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12],
            "kind": "author_guide", "content": content}


def import_document(store, source_id, document, *, origin, entities=None, topics=None, license_note="rights retained by original author"):
    """Only the source layer is updated. The existing approval gate remains mandatory."""
    return store.import_source(source_id, game_version=DOCUMENT_SCOPE, locale="zh", title=document["title"], origin=origin,
                               media_type="application/json", raw=json.dumps(document, ensure_ascii=False, sort_keys=True).encode(),
                               metadata={"adapter": "dated_public_document_v1", "entities": sorted(set((entities or []) + ["document:" + source_id])), "topics": topics or [],
                                         "page_kinds": ["reference"], "source_date": document.get("source_date"),
                                         "date_basis": document.get("date_basis"), "document_version": document["document_version"],
                                         "applicable_game_version": document.get("game_version"), "author": document.get("author"),
                                         "license_note": license_note, "publication": "pending_compile_and_review"})
