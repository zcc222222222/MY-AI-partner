"""nanoka 装备详情归一化。武器和声骸参数矩阵方向不同,必须分别处理。"""
from .normalize import Normalized, _CLEAN, _as_int, _index, _json, _parameter_text


def normalize_weapon(data: dict, norm: Normalized) -> None:
    wid = int(data["id"])
    norm.add("weapon_details", {
        "weapon_id": wid, "name": data.get("name"), "desc": _CLEAN(data.get("desc")).text,
        "effect_name": data.get("effect_name"), "effect_template": data.get("effect"),
        "effect_params": _json(data.get("param")),
        "is_skin": bool(data.get("skin")),
    })
    p = _CLEAN(data.get("desc"))
    _index(norm, "weapon", str(wid), "detail.desc", p.text, p.refs)
    for phase, levels in (data.get("stats") or {}).items():
        for level, attributes in levels.items():
            for idx, attr in enumerate(attributes):
                norm.add("weapon_stats", {
                    "weapon_id": wid, "phase": int(phase), "level": int(level), "idx": idx,
                    "name": attr.get("name"), "raw_value": attr.get("value"),
                    "is_ratio": attr.get("is_ratio"), "is_percent": attr.get("is_percent"),
                })
    # param[参数编号][谐振阶级 - 1]。不把二维数组直接交给普通文本展开器。
    matrix = data.get("param") or []
    ranks = max((len(v) for v in matrix if isinstance(v, list)), default=0)
    for rank in range(ranks):
        values = [v[rank] if isinstance(v, list) and rank < len(v) else None for v in matrix]
        p = _parameter_text(norm, "weapon", str(wid), f"effect.{rank + 1}", data.get("effect"), values)
        if any(value is None for value in values):
            norm.note(f"武器 {wid} 谐振 {rank + 1}: 源参数数组缺值,对应占位符保留原文")
        norm.add("weapon_effects", {"weapon_id": wid, "refinement": rank + 1,
                                    "desc": p.text, "params": _json(values)})
    if not ranks:
        p = _CLEAN(data.get("effect"))
        _index(norm, "weapon", str(wid), "effect", p.text, p.refs)
    for phase, costs in (data.get("ascensions") or {}).items():
        for idx, cost in enumerate(costs):
            norm.add("weapon_ascensions", {"weapon_id": wid, "phase": int(phase), "idx": idx,
                                           "item_key": _as_int(cost.get("key")), "amount": _as_int(cost.get("value"))})
    _detail_alias(norm, "weapon", wid, data.get("name"))


def normalize_echo(data: dict, norm: Normalized) -> None:
    eid = int(data["id"])
    skill = data.get("skill") or {}
    p = _CLEAN(skill.get("simple_desc"))
    norm.add("echo_details", {
        "echo_id": eid, "name": data.get("name"), "type": data.get("type"),
        "intensity": data.get("intensity"), "place": data.get("place"),
        "intensity_code": data.get("intensity_code"), "monster_info": data.get("monster_info"),
        "rarity": _json(data.get("rarity")), "skill_template": skill.get("desc"),
        "simple_desc": p.text, "skill_params": _json(skill.get("param")), "damage": _json(skill.get("damage")),
        "phantom": _json(data.get("phantom")),
    })
    _index(norm, "echo", str(eid), "skill.simple", p.text, p.refs)
    # param[技能档位 - 1][参数编号]。不要套用武器的转置规则。
    matrix = skill.get("param") or []
    for rank, values in enumerate(matrix, start=1):
        if not isinstance(values, list):
            raise ValueError(f"声骸 {eid} 技能参数第 {rank} 行不是数组")
        p = _parameter_text(norm, "echo", str(eid), f"skill.{rank}", skill.get("desc"), values)
        norm.add("echo_skills", {"echo_id": eid, "skill_rank": rank, "desc": p.text, "params": _json(values)})
    if not matrix:
        p = _CLEAN(skill.get("desc"))
        _index(norm, "echo", str(eid), "skill", p.text, p.refs)
    for sid, group in (data.get("group") or {}).items():
        norm.add("echo_sonatas", {"echo_id": eid, "sonata_id": int(sid), "name": group.get("name")})
        for pieces, effect in (group.get("set") or {}).items():
            p = _parameter_text(norm, "echo", str(eid), f"sonata.{sid}.{pieces}", effect.get("desc"), effect.get("param"))
            norm.add("echo_sonata_effects", {"echo_id": eid, "sonata_id": int(sid), "pieces": int(pieces),
                                             "desc": p.text, "params": _json(effect.get("param"))})
    _detail_alias(norm, "echo", eid, data.get("name"))
    phantom = data.get("phantom")
    if isinstance(phantom, dict):
        _detail_alias(norm, "echo", eid, phantom.get("name"))


def _detail_alias(norm, kind, eid, name):
    if name:
        norm.add("aliases", {"alias": name.strip(), "entity_type": kind, "entity_id": str(eid),
                             "locale": "", "source": "detail.name"})
