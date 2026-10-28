"""Adapters for the audited public source pages and endpoints."""
import hashlib
import json
import math
import re

from .collector import CollectionError, chinese, import_document, html_text, mcguide_document


def emit(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


def newest_chinese_guides(rows):
    """Platform modifiedAt is the only date basis; equal dates use stable numeric IDs."""
    selected = [r for r in rows if (chinese(r.get("texts")) or {}).get("introductionName", "").strip()]
    def key(row):
        stamp = row.get("modifiedAt")
        stamp = float(stamp) if type(stamp) in (int, float) and math.isfinite(stamp) and stamp >= 0 else -1
        return stamp, int(row["id"])
    return sorted(selected, key=key, reverse=True)


def collect_mcguide(client, store, roles, limit, report, progress=None):
    base = "https://guide-server.aki-game.com"
    catalog = client.json(base + "/role/avatar/list")
    if catalog.get("code") != 200 or not isinstance(catalog.get("data"), list):
        raise CollectionError("Invalid role catalog")
    catalog = {str(row["roleGbId"]): row for row in catalog["data"]}
    for role in roles:
        if role not in catalog:
            report["skipped"].append({"role": role, "reason": "not_in_official_guide_catalog"})
            continue
        listing = client.json(base + f"/introduction/list?roleGbId={role}")
        if listing.get("code") != 200 or not isinstance(listing.get("data"), list):
            raise CollectionError("Invalid introduction listing")
        ordered = newest_chinese_guides(listing["data"])
        selected = ordered[:limit]
        report.setdefault("selections", []).append({"role": role, "selected_ids": [r["id"] for r in selected],
            "date_basis": "platform_modifiedAt", "tie_breaker": "numeric_id_desc_not_publication_date",
            "candidates": [{"id": r["id"], "modifiedAt": r.get("modifiedAt")} for r in ordered]})
        if not selected:
            report["skipped"].append({"role": role, "reason": "no_chinese_guides_in_public_listing"})
        for row in selected:
            endpoint = base + f"/introduction/info?roleGbId={role}&id={row['id']}"
            detail = client.json(endpoint)
            if detail.get("code") != 200 or not isinstance(detail.get("data"), dict):
                raise CollectionError("Invalid introduction detail")
            doc = mcguide_document(row, detail["data"])
            doc["role_id"] = role
            doc["detail_url"] = endpoint
            result = import_document(store, f"mcguide/{role}/{row['id']}", doc,
                                     origin="https://mcguide.kurogames.com/", entities=[f"character:{role}"],
                                     topics=["author-guide", "build", "team"])
            report["imported"].append({"site": "mcguide", "title": doc["title"], "date": doc["source_date"], **result})
            (progress or emit)(report["imported"][-1])


def collect_official(client, store, limit, report, article_ids=None, progress=None):
    # Public JSON paths observed in the official site's frontend; no account API.
    base = "https://media-cdn-mingchao.kurogame.com/akiwebsite/website2.0/json/G152/zh"
    menu = client.json(base + "/ArticleMenu.json")
    if not isinstance(menu, list):
        raise CollectionError("Invalid official article menu")
    if article_ids:
        known = {row.get("articleId") for row in menu}
        for article_id in article_ids:
            if article_id not in known:
                report["skipped"].append({"article_id": article_id, "reason": "not_in_public_official_menu"})
    selected = sorted([row for row in menu if (row.get("articleId") in article_ids if article_ids else
                      re.search("版本内容说明|更新维护预告|全平台活动开启|客户端资源", row.get("articleTitle", "")))],
                      key=lambda row: row.get("startTime", ""), reverse=True)[:limit]
    for row in selected:
        article = client.json(base + f"/article/{int(row['articleId'])}.json")
        if not isinstance(article, dict) or not article.get("articleContent"):
            raise CollectionError("Official article has no full body")
        title = article.get("articleTitle", row["articleTitle"])
        date = article.get("startTime", row.get("startTime"))
        version = re.search(r"(?<!\d)(\d+\.\d+)版本", title)
        digest = hashlib.sha256(json.dumps(article, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        doc = {"schema": "dated-source-v1", "kind": "official_notice", "title": title, "author": "鸣潮官方",
               "source_date": date, "date_timezone": "Asia/Shanghai", "date_basis": "official_startTime",
               "document_version": (date or "undated") + "/" + digest[:12], "game_version": version[1] if version else None,
               "content": article, "readable_text": html_text(article["articleContent"])}
        result = import_document(store, f"official/news/{row['articleId']}", doc,
                                 origin=f"https://mc.kurogames.com/main/news/detail/{row['articleId']}", topics=["official-notice"])
        report["imported"].append({"site": "official", "title": title, "date": date, "game_version": doc["game_version"], **result})
        (progress or emit)(report["imported"][-1])


def main_body(html):
    match = re.search(r'<div\b[^>]*class="[^"]*\bmw-parser-output\b[^"]*"[^>]*>', html)
    if not match:
        raise CollectionError("Missing MediaWiki article body")
    return html[match.end():].split("<!-- NewPP limit report", 1)[0].split('<!--\nNewPP limit report', 1)[0].split('<div class="printfooter"', 1)[0]


def bwiki_doc(title, fragment, *, page_revision=None):
    from .images import InlineImages
    parser = InlineImages()
    parser.feed(fragment)
    plain = "".join(parser.parts)
    images = [{"url": i["raw_url"], "alt": i["alt"],
               "nearby_text": re.sub(r"(?:已开始|已结束)?\d+天\d+时(?:后结束|后开启)?", "", plain[max(0,i["offset"]-160):i["offset"]+190]).strip()}
              for i in parser.images if i["raw_url"]][:100]
    text = html_text(fragment)
    # Server-rendered countdowns are volatile; keep absolute event times instead.
    text = "\n".join(line for line in text.splitlines() if not re.fullmatch(r"(?:已开始|已结束)?\d+天\d+时(?:后结束|后开启)?", line))
    periods = [{"start": a, "end": b, "timezone": "Asia/Shanghai"} for a, b in
               re.findall(r'data-start="([^"]+)"[^>]*data-end="([^"]+)"', fragment)]
    digest = hashlib.sha256(json.dumps({"text": text, "periods": periods, "images": images}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {"schema": "dated-source-v1", "kind": "community_event_or_endgame", "title": title,
            "author": "鸣潮BWIKI页面贡献者", "source_date": None, "date_basis": "section_update_date_not_available",
            "document_version": "sha256/" + digest, "game_version": None, "periods": periods,
            "parent_page_revision": page_revision, "readable_text": text, "original_images": images,
            "applicability_note": "周期只适用于本段明确列出的时间；父页面修订日期不代表嵌入内容更新日期。"}


def bwiki_endgame_sections(html):
    """Published mode headings, bounded at their own timer, never sidebar/event dates."""
    marker = r'<div style="margin:10px;font-weight:600"><div[^>]*></div>'
    sections = []
    for title in ('逆境深塔','冥歌海墟','终焉矩阵'):
        match = re.search(marker + re.escape(title) + r'</div>',html)
        if not match: continue
        boundaries = [pos for pos in (html.find('<hr',match.end()),
            html.find('<div class="col-sm-12 col-sm-12 col-md-3 col-lg-3">',match.end())) if pos >= 0]
        next_heading = re.search(marker,html[match.end():])
        if next_heading: boundaries.append(match.end()+next_heading.start())
        end = min(boundaries) if boundaries else len(html)
        fragment = html[match.start():end]
        timer = re.search(r'<span\b[^>]*data-start="[^"]+"[^>]*data-end="[^"]+"[^>]*></span>',fragment)
        if not timer: raise CollectionError('Published mode lacks a bounded absolute timer: '+title)
        sections.append((title,fragment[:timer.end()]))
    return sections


def collect_bwiki(client, store, limit, report, progress=None):
    root = "https://wiki.biligame.com/wutheringwaves/"
    raw, _, _ = client.fetch(root + "首页")
    html = raw.decode("utf-8")
    revision = re.search(r'"wgRevisionId":(\d+)', html)
    revision = int(revision[1]) if revision else None
    sections = []
    published = dict(bwiki_endgame_sections(html))
    # Only the published home page, never the Aion sandbox/test page.
    for title in ("逆境深塔", "冥歌海墟", "终焉矩阵"):
        if title not in published:
            report["skipped"].append({"section": title, "reason": "not_in_published_home_page"})
            continue
        sections.append(("首页/" + title,published[title],root + "首页"))
    # Public transcluded subpage observed in the home page's parser report.
    if len(sections) < limit:
        raw, _, _ = client.fetch(root + "首页/活动日历")
        sections.append(("首页/活动日历", main_body(raw.decode("utf-8")), root + "首页/活动日历"))
    for title, fragment, origin in sections[:limit]:
        doc = bwiki_doc(title, fragment, page_revision=revision if origin.endswith("首页") else None)
        result = import_document(store, "bwiki/" + title, doc, origin=origin, topics=["events" if "日历" in title else "endgame"],
                                 license_note="鸣潮BWIKI贡献者；CC BY-NC-SA 4.0（适用范围以页面声明为准）；官方素材权利归库洛；保留来源及署名")
        report["imported"].append({"site": "bwiki", "title": title, "periods": doc["periods"], **result})
        (progress or emit)(report["imported"][-1])
