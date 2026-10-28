"""Rebuildable original-image associations. No image download, OCR or guessed URLs."""
import hashlib
from html.parser import HTMLParser
import json
import re
from urllib.parse import urljoin, urlsplit, unquote

from .collector import DOCUMENT_SCOPE, chinese, html_text
from .source_links import readable_source_url

IMAGE_ROUTE = "/api/v1/knowledge/images/"
IMAGE_HOSTS = {"guide-res.aki-game.com": ("/introduction/material/",),
               "patchwiki.biligame.com": ("/images/wutheringwaves/",),
               "media-cdn-mingchao.kurogame.com": ("/akiwebsite/", "/object/"),
               "i0.hdslb.com": ("/bfs/",), "i1.hdslb.com": ("/bfs/",), "i2.hdslb.com": ("/bfs/",)}


def public_image_url(url, base=""):
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 32 for c in url) or "\\" in url:
        return None
    url = urljoin(base, url.strip())
    try:
        p = urlsplit(url)
        path = unquote(p.path)
        if (p.scheme != "https" or p.hostname not in IMAGE_HOSTS or p.username or p.password or p.port
                or p.fragment or any(s in (".", "..") for s in path.split("/"))
                or not any(path.startswith(prefix) for prefix in IMAGE_HOSTS[p.hostname])
                or not re.search(r"\.(?:png|jpe?g|webp|gif)(?:@[^/]*)?$", path, re.I)):
            return None
    except ValueError:
        return None
    return url


class InlineImages(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.images, self.parts, self.skip = [], [], 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        if tag == "img" and not self.skip:
            self.images.append({"raw_url": a.get("data-src") or a.get("data-original") or a.get("src"),
                                "alt": (a.get("alt") or "")[:100], "offset": len("".join(self.parts))})
        if tag in ("p", "div", "br", "li", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def extract_images(source_id, revision, doc, origin):
    result, seen = [], set()
    def add(url, path, anchor, label, kind, nearby="", name="", ordinal=None):
        url = public_image_url(url, origin)
        if not url or (url, json.dumps(path), ordinal) in seen or len(result) >= 100:
            return
        seen.add((url, json.dumps(path), ordinal))
        raw = json.dumps([DOCUMENT_SCOPE, "zh", source_id, revision, path, ordinal, url], ensure_ascii=False, separators=(",", ":"))
        result.append({"image_id": hashlib.sha256(raw.encode()).hexdigest(), "url": url,
                       "source_path": path, "anchor_path": anchor, "label": label[:100], "kind": kind,
                       "nearby_text": nearby[:350], "name": name[:100], "image_ordinal": ordinal})
    def inline(value, path, anchor=None):
        parser = InlineImages()
        parser.feed(value or "")
        text = "".join(parser.parts)
        for i, img in enumerate(parser.images):
            offset = img["offset"]
            nearby = text[max(0,offset-160):offset+190].strip()
            add(img["raw_url"], path, anchor or path, img["alt"] or "原文配图（图中文字未识别）",
                "article_image", nearby, ordinal=i)
    content = doc.get("content") or {}
    if doc.get("kind") == "author_guide":
        for group in ("baseTexts", "echoTexts", "weaponTexts", "roleResonanceTexts"):
            for i, row in enumerate(content.get(group) or []):
                if row.get("language") == "zh-Hans":
                    for key in ("roleDescription", "introductionDescription", "introductionSynopsis", "introductionDetail", "recommendDescription"):
                        if isinstance(row.get(key),str):
                            inline(row[key], ["content", group, i, key])
        def material(node, path, kind, prefix):
            if not isinstance(node,dict): return
            name = (chinese(node.get("texts")) or {}).get("name", "").strip()
            if name:
                key = "pictureUrl" if node.get("pictureUrl") else "cardPictureUrl"
                add(node.get(key),path + [key],path,prefix+name,kind,name=name)
        for i,node in enumerate((content.get("weapon") or {}).get("items") or []):
            material(node,["content","weapon","items",i],"weapon_material","武器素材：")
        for i,slot in enumerate((content.get("teammate") or {}).get("items") or []):
            material(slot.get("main"),["content","teammate","items",i,"main"],"teammate_material","队友素材：")
            for j,node in enumerate(slot.get("spares") or []):
                material(node,["content","teammate","items",i,"spares",j],"teammate_material","队友候选素材：")
        for key in ("main","spare"):
            material(((content.get("echo") or {}).get(key) or {}).get("echoProps"),["content","echo",key,"echoProps"],"echo_material","声骸素材：")
        role = content.get("role") or {}
        name = (chinese(role.get("texts")) or {}).get("name", "")
        key = "illustrationPictureUrl" if role.get("illustrationPictureUrl") else "cardPictureUrl"
        add(role.get(key),["content","role",key],
            ["content","role"],"角色立绘素材："+name,"character_illustration",name=name)
    elif doc.get("kind") == "official_notice":
        inline(content.get("articleContent", ""), ["content","articleContent"], ["readable_text"])
    elif doc.get("media_html"):
        inline(doc["media_html"], ["media_html"], ["readable_text"])
    for i,image in enumerate(doc.get("original_images",[])):
        add(image.get("url"),["original_images",i,"url"],["readable_text"],image.get("alt") or "原文配图（图中文字未识别）",
            "article_image",image.get("nearby_text", ""))
    return result


def index_source_images(conn, source_id, revision, text, origin):
    doc = json.loads(text)
    if not isinstance(doc,dict) or doc.get("schema") != "dated-source-v1": return 0
    assets = extract_images(source_id,revision,doc,origin)
    conn.execute("DELETE FROM document_images WHERE source_id=? AND game_version=? AND locale='zh' AND source_revision=?",
                 (source_id,DOCUMENT_SCOPE,revision))
    for asset in assets:
        conn.execute("INSERT INTO document_images VALUES (?,?,?,?,?,?)",
                     (asset["image_id"],source_id,DOCUMENT_SCOPE,"zh",revision,json.dumps(asset,ensure_ascii=False)))
    return len(assets)


def resolve_image(store, image_id):
    if not isinstance(image_id,str) or not re.fullmatch(r"[0-9a-f]{64}",image_id): return None
    store.refresh(game_version=DOCUMENT_SCOPE,locale="zh")
    with store.connection() as c:
        row = c.execute("SELECT i.*,r.text,r.origin,r.metadata FROM document_images i JOIN sources s "
            "USING(source_id,game_version,locale) JOIN source_revisions r ON r.source_id=i.source_id "
            "AND r.game_version=i.game_version AND r.locale=i.locale AND r.revision=i.source_revision "
            "WHERE i.image_id=? AND s.current_revision=i.source_revision AND r.deleted=0 AND EXISTS ("
            "SELECT 1 FROM source_dependencies dep JOIN pages p USING(page_id,game_version,locale) "
            "JOIN page_revisions pr ON pr.page_id=p.page_id AND pr.game_version=p.game_version AND pr.locale=p.locale AND pr.revision=p.active_revision "
            "JOIN drafts d ON d.page_id=p.page_id AND d.game_version=p.game_version AND d.locale=p.locale AND d.fingerprint=pr.fingerprint "
            "JOIN reviews v ON v.draft_id=d.draft_id AND v.fingerprint=d.fingerprint "
            "WHERE dep.source_id=i.source_id AND dep.game_version=i.game_version AND dep.locale=i.locale AND dep.source_revision=i.source_revision "
            "AND dep.page_revision=p.active_revision AND p.dirty=0 AND d.state='published' "
            "AND v.review_id=(SELECT MAX(review_id) FROM reviews WHERE draft_id=d.draft_id) AND v.decision='approved')",(image_id,)).fetchone()
        if not row: return None
        asset = json.loads(row["payload"])
        doc = json.loads(row["text"])
        # Re-derive the association from the pinned raw source, rather than trust a URL supplied by a model.
        if not any(a==asset for a in extract_images(row["source_id"],row["source_revision"],doc,row["origin"])): return None
        return {**asset,"source_id":row["source_id"],"source_revision":row["source_revision"],
                "origin":readable_source_url(doc.get("detail_url") or row["origin"]),"author":doc.get("author", ""),
                "source_date":doc.get("source_date"),"date_basis":doc.get("date_basis"),
                "license_note":json.loads(row["metadata"]).get("license_note", "")}


def images_for_page(store, page, query):
    from .search import rank, terms
    assets = []
    requested = any(word in query for word in ("图片","原图","配图","立绘","头像"))
    topics = {kind for kind, words in (
        ("weapon_material", ("武器",)),
        ("echo_material", ("声骸",)),
        ("teammate_material", ("配队", "队伍", "队友")),
    ) if any(word in query for word in words)}
    with store.connection() as c:
        for source in page["source_refs"]:
            rows = c.execute("SELECT payload FROM document_images WHERE source_id=? AND game_version=? AND locale='zh' AND source_revision=?",
                             (source["source_id"],DOCUMENT_SCOPE,source["revision"])).fetchall()
            for row in rows:
                a = json.loads(row[0])
                score = 0
                for match in page["matches"]:
                    anchors = [r.get("source_path",[]) for r in match["source_refs"] if r["source_id"]==source["source_id"] and r["revision"]==source["revision"]]
                    linked = any(p[:len(a["anchor_path"])]==a["anchor_path"] for p in anchors)
                    named = bool(a["name"] and a["name"] in match["text"])
                    if a["kind"] == "character_illustration":
                        if requested and not any(w in query for w in ("武器","声骸","配队","队伍","队友","手法","流程")): score = max(score,1)
                    elif a["kind"] == "article_image":
                        if requested or (linked and rank(a["nearby_text"]+a["label"],terms(query))): score=max(score,3)
                    elif (linked or named) and (not topics or a["kind"] in topics) and (not requested or topics or a["name"] in query):
                        score=max(score,2+rank(a["name"],terms(query)))
                if score:
                    assets.append((score,a,source))
    selected=[];seen=set()
    for _,a,source in sorted(assets,key=lambda t:-t[0]):
        if a["url"] in seen or resolve_image(store,a["image_id"]) is None: continue
        seen.add(a["url"])
        selected.append({"image_id":a["image_id"],"display_url":IMAGE_ROUTE+a["image_id"],"label":a["label"],"kind":a["kind"],
                         "source_id":source["source_id"],"source_revision":source["revision"],
                         "association":"source_field" if a["kind"]!="article_image" else "nearby_source_text_not_ocr"})
        if len(selected)==2: break
    return selected
