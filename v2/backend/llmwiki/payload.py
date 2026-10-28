"""A page owns one citation dictionary; claims keep stable citation references."""
import hashlib
import json
import re
from decimal import Decimal


KINDS = {"character_mechanics", "character_overview", "glossary", "story_digest", "qa_pairs", "version_digest", "reference", "team_guide"}


def citations_for(page, claim):
    if "citation_ids" in claim:
        return [page["citations"][key] for key in claim["citation_ids"]]
    return claim.get("citations", [])


def compact(page):
    """Accept legacy drafts, without modifying their original revision on disk."""
    page = json.loads(json.dumps(page, ensure_ascii=False))
    bibliography = {}
    for claim in page["claims"]:
        refs = []
        for citation in citations_for(page, claim):
            raw = json.dumps(citation, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            key = "cite-" + hashlib.sha256(raw.encode()).hexdigest()[:20]
            if key in bibliography and bibliography[key] != citation:
                raise ValueError("citation hash collision")
            bibliography[key] = citation
            if key not in refs:
                refs.append(key)
        claim.pop("citations", None)
        claim["citation_ids"] = refs
    page["citations"] = bibliography
    page["payload_version"] = 2
    page.setdefault("kind", "reference")
    if page["kind"] not in KINDS:
        raise ValueError("unsupported wiki kind")
    return page


def numbers(text):
    # Decimal normalization accepts 1.20 == 1.2, but never invents unit conversions.
    return {str(Decimal(m.group(1)).normalize()) + ("%" if m.group(2) else "")
            for m in re.finditer(r"(?<![0-9.])(\d+(?:\.\d+)?)([%％]?)", text)}


def citation_text(source_text, citation, parsed_source=None):
    """Resolve parameters from the actual cited JSON field, not arbitrary JSON numbers."""
    path = citation.get("source_path")
    if not path:
        return citation["quote"]
    from knowledge.parse import parse, resolve_params
    value = json.loads(source_text) if parsed_source is None else parsed_source
    parent = None
    for part in path:
        parent, value = value, value[part]
    if json.dumps(value, ensure_ascii=False) != citation["quote"]:
        raise ValueError("source_path does not identify quote")
    if path[-1] == "desc" and isinstance(parent, dict):
        value = resolve_params(value, parent.get("param"))
    return parse(str(value)).text + " " + " ".join(str(p) for p in path)
