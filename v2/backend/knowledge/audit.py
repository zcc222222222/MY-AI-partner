"""对镜像的实际字段做统计,发现上游新增字段;不依赖线上站点。"""
from collections import Counter

FIELDS = {
    "roster": "icon background rank weapon element en nickname desc ko zh ja",
    "character": "id rarity weapon element tag name nick_name desc icon background background_stand chara_info stories voices goods special_cook stats stats_weakness level_exp skill_trees skill_branches chains ascensions skin forte forte_new live2d audio recommend",
    "weapon": "icon rank type en desc atk sub ko zh ja",
    "echo": "icon code rank group en intensity ko zh ja phantom",
    "sonata": "id icon color name set",
    "monster": "icon rank element echo en ko zh ja",
    "item": "icon rank tag tagnum type name",
    "hyperlink": "name desc param",
    "skill_node": "parent_nodes node_type coordinate un_lock_condition skill_branch_ids consume skill",
    "skill": "name desc param simple_desc simple_param icon type level consume damage",
    "chain": "name desc param icon",
    "weapon_detail": "id rarity type name desc icon model stats effect effect_name param ascensions skin",
    "echo_detail": "id code name type intensity place icon skill group rarity intensity_code monster_info phantom",
    "echo_skill": "desc simple_desc param icon damage",
}


def inspect(kind: str, payload: dict) -> dict:
    entries = [payload] if kind in ("character", "weapon_detail", "echo_detail") else list(payload.values())
    counter = Counter(k for entry in entries if isinstance(entry, dict) for k in entry)
    known = set(FIELDS.get(kind, "").split())
    result = {"records": len(entries), "fields": dict(sorted(counter.items())),
              "unknown_fields": sorted(set(counter) - known)}
    if kind == "character":
        nodes = payload.get("skill_trees") or {}
        result["skill_node"] = inspect("skill_node", nodes)
        result["skill"] = inspect("skill", {k: n.get("skill") or {} for k, n in nodes.items()})
        result["chain"] = inspect("chain", payload.get("chains") or {})
    if kind == "echo_detail":
        result["skill"] = inspect("echo_skill", {"skill": payload.get("skill") or {}})
    return result
