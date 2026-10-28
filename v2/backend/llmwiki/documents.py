"""Dated source projections. Document revisions and game patches are separate scopes."""
from datetime import datetime, timezone, timedelta
import json
import re

from .collector import DOCUMENT_SCOPE, html_text
from .store import WikiError

PROJECTION = "dated-documents-v2"
RULES = """\n本次编译的是带日期的来源文档。documents-v1只是内部语料命名空间，不能写成游戏版本。
source_date若来自platform_modifiedAt，只称平台更新时间；不能称作者发布日期、复核版本或游戏实测日期。
日期一律按证据文字标明的服务器时间解释；系统用于筛选的UTC时间不改变原文服务器时间，不混淆时区。
applicable_game_version未标明时，不从日期推断游戏版本。作者攻略的推荐和机制解读均须标明作者观点，不称官方机制。
保留每套队伍的模式、位置和替代前提；推荐槽位只是界面候选，不推导所有候选任意组合均等效。配装列表不证明计算或最优。
一条结论只引用一个活动的证据；活动日期与开放条件写在同一结论内，不能将历史兑换码写成当前有效。
官方公告只概括提取到的文字，不臆测图片细则；版本更新后的活动开始时间保留原文，不换算成未核实时间。
未投影的技能面板、图片、视频不是不存在。对原文简略的描述保持简略，不依据外部记忆补齐。
程序给作者攻略每条结论统一添加“作者攻略观点：”前缀，已表示观点归属；审核不要因没有重复作者姓名而改写。修订时必须保留此前缀。
"""


def iso_time(value, *, end=False):
    if not value:
        return None
    value = value.strip().replace("/", "-")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})", value)
        if not match:
            raise WikiError("无法校验活动时间")
        dt = datetime(*map(int, match.groups()))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
    if end and len(value.rsplit(":", 1)[-1]) == 2:
        dt = dt.replace(second=59, microsecond=999999)
    return dt.astimezone(timezone.utc).isoformat()


def valid_at(periods, moment):
    return all((not p.get("start") or p["start"] <= moment) and
               (not p.get("end") or moment <= p["end"]) for p in periods)


def _entry(source, label, text, anchors, periods=None):
    return {"label": label, "text": text, "source_path": anchors[0][0], "periods": periods or [],
            "citations": [{"source_id": source["source_id"], "revision": source["revision"],
                           "source_path": path, "quote": json.dumps(value, ensure_ascii=False)} for path, value in anchors]}


def document_parts(source):
    data = json.loads(source["text"])
    if data.get("schema") != "dated-source-v1":
        raise WikiError("不支持的日期文档结构")
    entries = []
    def add(label, text, path, value, periods=None):
        if text.strip():
            entries.append(_entry(source, label, text, [(path, value)], periods))
    if data["kind"] == "author_guide":
        content = data["content"]
        for group in ("baseTexts", "echoTexts", "weaponTexts", "roleResonanceTexts"):
            for index, item in enumerate(content.get(group) or []):
                if item.get("language") != "zh-Hans":
                    continue
                for field in ("roleDescription", "introductionDescription", "introductionSynopsis", "introductionDetail", "recommendDescription"):
                    value = item.get(field)
                    if isinstance(value, str):
                        add(group + "/" + field, html_text(value), ["content", group, index, field], value)
        # Selection names are anchored to their own original scalar fields; no inferred stats.
        def names(value, path):
            result = []
            if not isinstance(value, dict):
                return result
            for i, item in enumerate(value.get("texts") or []):
                if item.get("language") == "zh-Hans" and item.get("name"):
                    result.append((path + ["texts", i, "name"], item["name"]))
            return result
        for i, slot in enumerate((content.get("teammate") or {}).get("items") or []):
            main = names(slot.get("main"), ["content", "teammate", "items", i, "main"])
            spare = [pair for j, item in enumerate(slot.get("spares") or []) for pair in names(item, ["content", "teammate", "items", i, "spares", j])]
            if main:
                text = "界面推荐队友槽位：主选" + "、".join(v for _, v in main)
                if spare:
                    text += "；同槽位候选" + "、".join(v for _, v in spare)
                entries.append(_entry(source, "队友候选（不是任意组合等效证明）", text, main + spare))
        for i, item in enumerate((content.get("weapon") or {}).get("items") or []):
            anchors = names(item, ["content", "weapon", "items", i])
            if anchors:
                entries.append(_entry(source, "界面武器候选", "界面列出的武器候选：" + "、".join(v for _, v in anchors), anchors))
        for key in ("main", "spare"):
            echo = (content.get("echo") or {}).get(key) or {}
            anchors = names(echo.get("echoProps"), ["content", "echo", key, "echoProps"])
            sets = []
            for i, item in enumerate(echo.get("echoSetEffects") or []):
                sets += names(item, ["content", "echo", key, "echoSetEffects", i])
            if anchors or sets:
                entries.append(_entry(source, "界面声骸方案/" + key,
                    "声骸方案" + key + "：首位候选" + "、".join(v for _, v in anchors) + "；合鸣效果名称" + "、".join(sorted({v for _, v in sets})), anchors + sets))
    elif data["kind"] == "community_event_or_endgame" and "活动日历" in data["title"]:
        # Keep each event's complete name/time together, including permanent or expired entries.
        blocks = re.split(r"(?=^「)", data["readable_text"], flags=re.M)
        for block in blocks:
            if not block.strip():
                continue
            times = re.findall(r"\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}", block)
            if not times:
                raise WikiError("日历条目缺少可校验的绝对时间")
            periods = [{"start": iso_time(times[0]), "end": iso_time(times[1], end=True) if len(times) > 1 else None}]
            add("活动/" + block.splitlines()[0], block.strip(), ["readable_text"], data["readable_text"], periods)
    else:
        text = data.get("readable_text", "")
        # Official headings retain the whole following section. Never clip a sentence/condition.
        blocks = re.split(r"(?=^✦[^\n]*✦$|^【(?:维护时间＆补偿说明|版本内容介绍|功能调整|优化调整|问题修复)[^\n]*】$)", text, flags=re.M) if data["kind"] == "official_notice" else [text]
        periods = [{"start": iso_time(p["start"]), "end": iso_time(p["end"], end=True)} for p in data.get("periods", [])]
        for block in blocks:
            if block.strip():
                add(block.strip().splitlines()[0][:70], block.strip(), ["readable_text"], text, periods)
    if not entries:
        raise WikiError("文档无可编译的中文文字")
    parts, current, size = [], [], 0
    for entry in entries:
        if len(entry["text"]) > 6500:
            raise WikiError("单段超过6500字符，须按语义小节继续拆分，未截断条件")
        if current and (size + len(entry["text"]) > 6500 or len(current) >= 22):
            parts.append(current)
            current, size = [], 0
        current.append(entry)
        size += len(entry["text"])
    if current:
        parts.append(current)
    return data, parts


def metadata(source, data, part, count):
    from .source_links import readable_source_url
    category = ("guide" if data["kind"] == "author_guide" else "official" if data["kind"] == "official_notice"
                else "event" if "活动日历" in data["title"] else "endgame")
    result={"source_id": source["source_id"], "source_revision": source["revision"], "category": category,
            "source_date": data.get("source_date"), "date_basis": data.get("date_basis"),
            "document_version": data["document_version"], "applicable_game_version": data.get("game_version"),
            "author": data.get("author"), "origin": readable_source_url(data.get("detail_url") or source["origin"]),
            "entity_ids": [e for e in source["metadata"].get("entities", []) if not e.startswith("document:")],
            "license_note": source["metadata"].get("license_note"), "part": part, "parts": count,
            "coverage_note": "中文正文与明确候选配置；未投影的技能面板、图片和视频需另查原文。",
            "periods": [{"start": iso_time(p["start"]), "end": iso_time(p["end"], end=True)} for p in data.get("periods", [])]}
    if data.get('source_role')=='historical_supplement':
        if data.get('kind')!='author_guide' or not data.get('game_version') or not data.get('supplement_for'):
            raise WikiError('历史补充来源须有明确自述版本及关联来源')
        result.update(source_role='historical_supplement',supplement_for=data['supplement_for'],
                      supplement_note='独立历史作者资料；版本为来源自述，平台展示日期不证明版本或实测日期。未验证当前适用，也不表示关联原篇已修复。')
    return result


def prepare_metadata(store, template, part=0):
    if template["game_version"] != DOCUMENT_SCOPE:
        return
    if template["kind"] != "reference" or len(template["sources"]) != 1:
        raise WikiError("日期文档必须逐来源编译为reference，不能混编多作者或多个周期")
    ref = template["sources"][0]
    source = store.get_source(ref["source_id"], game_version=DOCUMENT_SCOPE, locale=template["locale"], revision=ref["revision"])
    data, parts = document_parts(source)
    if type(part) is not int or not 0 <= part < len(parts):
        raise WikiError("无效文档分章编号")
    template["document_metadata"] = metadata(source, data, part, len(parts))


def evidence(source, template):
    data, parts = document_parts(source)
    meta = template["document_metadata"]
    if meta != metadata(source, data, meta["part"], len(parts)):
        raise WikiError("文档元数据与锁定来源不一致")
    entries = parts[meta["part"]]
    # Context always accompanies every chapter and participates in exact source citations.
    anchors = [([key], data.get(key)) for key in ("author", "source_date", "game_version")]
    context = _entry(source, "来源与日期", f"作者/发布方：{data.get('author')}；日期：{data.get('source_date')}；日期依据：{data.get('date_basis')}；明确适用游戏版本：{data.get('game_version') or '未标明'}。", anchors)
    temporal = []
    for i, period in enumerate(data.get("periods", [])):
        anchors = [(["periods", i, key], period[key]) for key in ("start", "end")]
        temporal.append(_entry(source, "玩法周期", f"该玩法分段列明周期为{period['start']}至{period['end']}（Asia/Shanghai），只适用于该周期。", anchors, meta["periods"]))
    result=[context,*temporal,*entries]
    # Explicit curation-only append preserves every pre-existing evidence ID,
    # chapter boundary and ordinary compiler projection.
    for path in template.get('guide_curation',{}).get('extra_source_paths',[]):
        if (data.get('kind')!='author_guide' or len(path)!=4 or path[:2]!=['content','baseTexts']
                or type(path[2])!=int or path[2]<0 or path[3]!='skillDisplayDescription'):
            raise WikiError('未知攻略补充字段')
        item=data['content']['baseTexts'][path[2]];value=item.get(path[3])
        if item.get('language')!='zh-Hans' or not isinstance(value,str) or not html_text(value).strip():
            raise WikiError('攻略补充字段不是中文文字')
        if any(e['source_path']==path for e in result):raise WikiError('重复攻略补充证据')
        result.append(_entry(source,'baseTexts/skillDisplayDescription',html_text(value),[(path,value)]))
    return result


def validate_page(source, page):
    """Bind dates, chapter and per-claim validity to immutable source evidence."""
    entries = evidence(source, page)
    from .guide_curation import validate_curation
    validate_curation(source,page)
    from .guide_quality import validate_quality
    validate_quality(source,page)
    from .guide_followup import validate_followup
    validate_followup(source,page)
    from .notice_impacts import validate as validate_notice_impacts
    validate_notice_impacts(source,page)
    by_id = {f"E{i:03d}": e for i, e in enumerate(entries, 1)}
    from .payload import citations_for
    for claim in page["claims"]:
        if page["document_metadata"]["category"] == "guide" and not claim["text"].startswith("作者攻略观点："):
            raise WikiError("作者攻略结论丢失观点归属")
        ids = claim.get("evidence_ids", [])
        if not ids or any(key not in by_id for key in ids):
            raise WikiError("日期文档引用了未知证据")
        periods = [by_id[key]["periods"] for key in ids if by_id[key]["periods"]]
        if len({json.dumps(p, sort_keys=True) for p in periods}) > 1:
            raise WikiError("一条结论混用了不同活动周期")
        expected = periods[0] if periods else page["document_metadata"].get("periods", [])
        if claim.get("valid_periods") != expected:
            raise WikiError("结论活动时间与来源证据不一致")
        cited = {json.dumps(c, ensure_ascii=False, sort_keys=True) for c in citations_for(page, claim)}
        required = {json.dumps(c, ensure_ascii=False, sort_keys=True) for key in ids for c in by_id[key]["citations"]}
        if cited != required:
            raise WikiError("日期文档结论的引用锚点与证据编号不一致")
