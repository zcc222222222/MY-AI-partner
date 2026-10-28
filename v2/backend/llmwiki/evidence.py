"""Bounded compiler views with exact anchors back into pinned source text."""
import json

from knowledge.parse import parse, resolve_params
from .store import WikiError

PROJECTION_VERSION = "typed-views-v2"


def build_evidence(store, job):
    template = job["draft_template"]
    scope = {"game_version": template["game_version"], "locale": template["locale"]}
    evidence, omissions = [], []

    def add(source, label, text, path, quotes):
        if not text.strip():
            return
        citations = []
        for quote_path, quote in quotes:
            if quote not in source["text"]:
                raise WikiError("证据投影无法定位到原文，停止编译")
            citations.append({"source_id": source["source_id"], "revision": source["revision"],
                              "quote": quote, "source_path": quote_path})
        evidence.append({"evidence_id": f"E{len(evidence) + 1:03d}", "label": label,
                         "text": text, "source_path": path, "citations": citations})

    def field(source, label, data, path):
        raw = data.get("desc") or ""
        if not isinstance(raw, str) or not raw:
            return
        params = data.get("param") or []
        readable = parse(resolve_params(raw, params)).text
        anchors = [(path + ["desc"], json.dumps(raw, ensure_ascii=False))]
        for index in parse(raw).params:
            if isinstance(params, list) and index < len(params) and isinstance(params[index], (str, int, float)):
                anchors.append((path + ["param", index], json.dumps(params[index], ensure_ascii=False)))
        add(source, label, readable, path, anchors)

    for ref in template["sources"]:
        current = store.get_source(ref["source_id"], **scope)
        if current["revision"] != ref["revision"] or current["deleted"]:
            raise WikiError("编译输入来源已经变化，请重新prepare")
        source = store.get_source(ref["source_id"], revision=ref["revision"], **scope)
        kind = template.get("kind", "character_mechanics")
        if source["metadata"].get("adapter") == "dated_public_document_v1":
            from .documents import evidence as document_evidence
            if not template.get("document_metadata"):
                raise WikiError("日期文档缺少受校验的来源范围，停止编译")
            for item in document_evidence(source, template):
                if any(c["quote"] not in source["text"] for c in item["citations"]):
                    raise WikiError("日期文档证据无法定位原文")
                evidence.append({**item, "evidence_id": f"E{len(evidence) + 1:03d}"})
            omissions.append({"source_id": source["source_id"], "note": template["document_metadata"]["coverage_note"]})
            continue
        if kind == "team_guide":
            from .guides import guide_evidence
            for label, text, path, quotes in guide_evidence(source):
                add(source, label, text, path, quotes)
            continue
        if source["media_type"] == "application/json" and kind == "glossary":
            data = json.loads(source["text"])
            wanted = {tag.split(":", 1)[1] for tag in template.get("entities", []) if tag.startswith("hyperlink:")}
            if not wanted:
                raise WikiError("术语编译须指定hyperlink:<id>，不将整个词条库塞进模型")
            for key in sorted(wanted):
                item = data.get(key)
                if isinstance(item, dict):
                    for name, text in item.items():
                        if isinstance(text, str) and text.strip():
                            add(source, f"术语{key}/{name}", parse(text).text, [key, name],
                                [([key, name], json.dumps(text, ensure_ascii=False))])
            continue
        if source["media_type"] == "application/json" and source["metadata"].get("adapter") == "nanoka_raw_docs" and ref["source_id"].startswith("nanoka/character/") and kind in ("character_mechanics", "character_overview", "story_digest"):
            data = json.loads(source["text"])
            if kind == "story_digest":
                for group in ("stories", "voices", "goods"):
                    for key, item in (data.get(group) or {}).items():
                        if isinstance(item, dict):
                            for name, text in item.items():
                                if isinstance(text, str) and text.strip():
                                    add(source, f"{group}/{key}/{name}", parse(text).text, [group, key, name],
                                        [([group, key, name], json.dumps(text, ensure_ascii=False))])
                continue
            if kind == "character_overview":
                def overview_value(value, path):
                    if isinstance(value, dict):
                        for key, child in value.items():
                            overview_value(child, path + [key])
                    elif isinstance(value, list):
                        for index, child in enumerate(value):
                            overview_value(child, path + [index])
                    elif isinstance(value, (str, int, float)):
                        text = parse(str(value)).text
                        add(source, "/".join(map(str, path)), text, path,
                            [(path, json.dumps(value, ensure_ascii=False))])
                for key in ("name", "nick_name", "desc", "chara_info", "tag", "recommend"):
                    if key in data:
                        overview_value(data[key], [key])
            for key, node in (data.get("skill_trees") or {}).items():
                skill = node.get("skill") or {}
                field(source, f"技能节点{key}：{skill.get('name', '')}", skill, ["skill_trees", key, "skill"])
            for key, chain in (data.get("chains") or {}).items():
                field(source, f"共鸣链{key}：{chain.get('name', '')}", chain, ["chains", key])
            for key, branch in (data.get("skill_branches") or {}).items():
                field(source, f"共鸣模态：{branch.get('name', '')}", branch, ["skill_branches", key])
            omissions.append({"source_id": ref["source_id"],
                              "excluded": ["stories", "voices", "stats", "skill.level", "damage", "ascensions", "assets", "forte_new简化指引"],
                              "note": "机制视图只使用完整技能描述、共鸣链与模态；不据此计算伤害或输出轴，也不把未选字段视作不存在。"})
        else:
            # Generic documents are kept whole: never clip a condition to fit a budget.
            add(source, source["title"], source["text"], [], [([], source["text"])])
    if not evidence:
        raise WikiError("没有可编译的证据，请先导入资料并设置实体/主题标签")
    return evidence, omissions
