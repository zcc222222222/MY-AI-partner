"""源 JSON -> 表行。

放在 normalize 而不是直接塞进 build,是为了让这一层**纯粹**:不碰网络、不碰数据库,
只做数据形状转换。它可以对着 tests/fixtures/ 里的固定样本反复跑,
不需要联网也不需要真库。

两个原则:

1. **没有把握的字段不猜。** 源数据里有些形状我没确认(item.json 的完整 schema、
   ``{n}`` 参数的取值来源)。这些一律不编造,而是记进 report,让调用方看得见。
   静默猜错比缺字段危害大得多 —— 缺字段会报错,猜错会变成"看起来很确定"的错误答案。
2. **原文与解析结果并存。** 每个文本字段都从 parse() 出来,同时把引用 id 收集成边。
"""
from dataclasses import dataclass, field
from typing import Any

from . import config, parse

# 文本字段的清洗统一走这里 —— 集中一处,以后改规则不会漏掉某个表。
# 它容忍"字段可能是多语言 dict"这件事:源数据里 sonata.name 是 dict
# 而 item.name 是 str,直接喂给解析器会 TypeError 把整次构建打断。
def _CLEAN(value, locale: str = "zh") -> parse.ParsedText:
    return parse.parse(_text(value, locale))


def _parameter_text(norm, etype: str, eid: str, field_name: str, raw, values):
    """保存原文和参数,可读文本显式展开;引用仍从原文提取。"""
    p = _CLEAN(raw)
    norm.add("text_sources", {
        "entity_type": etype, "entity_id": eid, "field": field_name,
        "raw_text": _text(raw), "params": _json(values),
    })
    p.text = parse.resolve_params(p.text, values)
    _index(norm, etype, eid, field_name, p.text, p.refs)
    return p


@dataclass
class Normalized:
    """归一化结果。tables 里每个值是"要插进某张表的行列表"。"""

    tables: dict[str, list[dict]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def add(self, table: str, row: dict) -> None:
        self.tables.setdefault(table, []).append(row)

    def extend(self, table: str, rows: list[dict]) -> None:
        if rows:
            self.tables.setdefault(table, []).extend(rows)

    def note(self, msg: str) -> None:
        if msg not in self.notes:
            self.notes.append(msg)

    def skip(self, msg: str) -> None:
        if msg not in self.skipped:
            self.skipped.append(msg)

    def count(self, table: str) -> int:
        return len(self.tables.get(table, []))


def load_map(path: str) -> dict[int, str]:
    """读 maps/*.json。文件不存在或没填就返回空表 —— 缺映射不该让整个构建失败。"""
    import json
    import os

    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return {int(k): v.get("name", "") for k, v in (data.get("map") or {}).items()}


# --------------------------------------------------------------------------- 小工具


def _pick(d: dict, *keys: str, default=None):
    """按顺序取第一个存在的键。源数据字段名不稳定,硬取值会在某天炸掉。"""
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _as_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _json(v) -> str:
    import json

    return json.dumps(v, ensure_ascii=False) if v is not None else ""


def _localized(value, locale: str = "zh") -> str:
    """取多语言字段的某个语言。

    源数据里同名字段的类型在不同文件里并不一致:
      character.json / weapon.json / sonata.json 的名称是 {en,ko,zh,ja} 的 dict
      item.json / hyperlink.json(在 {locale}/ 下)的 name 直接就是字符串
    所以"取名称"这件事必须走这里,不能到处写 d["zh"],那会在单语言文件上炸掉。
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in (locale, "zh", "en", "ja", "ko"):
            if isinstance(value.get(key), str):
                return value[key]
    return ""


def _text(value, locale: str = "zh") -> str:
    """把字段安全地转成待解析的文本。非字符串一律降级成空串,绝不抛异常。

    整次构建不该因为一个字段的类型意外而失败 —— 静默丢一个字段是可接受的,
    静默丢整个知识库不是。
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in (locale, "zh", "en", "desc", "name"):
            if isinstance(value.get(key), str):
                return value[key]
    return ""


# --------------------------------------------------------------------------- 角色


def normalize_characters(
    roster: dict, details: dict[str, dict], version: str, norm: Normalized
) -> None:
    """roster 提供全量角色与多语言名;details 提供 id -> 详情(可能只有一部分)。"""
    elements = load_map(config.ELEMENT_MAP_FILE)
    weapons = load_map(config.WEAPON_MAP_FILE)

    for cid, entry in roster.items():
        element = _as_int(_pick(entry, "element"))
        weapon = _as_int(_pick(entry, "weapon"))
        detail = details.get(str(cid)) or {}

        # 详情优先:roster 的 desc 与详情里的 desc 内容一致,但详情更全。
        name = _pick(detail, "name") or _pick(entry, "zh")
        nick = _pick(detail, "nick_name") or _pick(entry, "nickname")

        tags = []
        for tag in (detail.get("tag") or {}).values():
            tags.append(_pick(tag, "name", default=""))
        tag_summary = "、".join(t for t in tags if t)

        norm.add(
            "characters",
            {
                "id": _as_int(cid),
                "name": name,
                "nick_name": nick,
                "rarity": _as_int(_pick(detail, "rarity") or _pick(entry, "rank")),
                "element": element,
                "weapon": weapon,
                "element_name": elements.get(element, ""),
                "weapon_name": weapons.get(weapon, ""),
                "game_version": version,
                "tag_summary": tag_summary,
            },
        )

        # 多语言名全部进别名表 —— 中文 2 字实体名靠它精确匹配,不走全文检索。
        sex = (detail.get("chara_info") or {}).get("sex")
        if name.startswith("漂泊者") and sex in ("男", "女"):
            for alias in (f"{name}·{sex}", f"{name}（{sex}）", f"{sex}{name}"):
                norm.add("aliases", {"alias": alias, "entity_type": "character", "entity_id": str(cid), "locale": "zh", "source": "name_and_profile_sex"})
        for alias, src in (
            (_pick(entry, "zh"), "name_zh"),
            (_pick(entry, "en"), "name_en"),
            (_pick(entry, "ja"), "name_ja"),
            (_pick(entry, "ko"), "name_ko"),
            (_pick(entry, "nickname"), "nickname"),
            (nick, "nick_name"),
        ):
            if alias:
                norm.add(
                    "aliases",
                    {
                        "alias": str(alias).strip(),
                        "entity_type": "character",
                        "entity_id": str(cid),
                        "locale": "",
                        "source": src,
                    },
                )

        from .reviewed_aliases import character_aliases
        norm.extend('aliases',character_aliases(cid,name))
        desc = _CLEAN(_pick(detail, "desc") or _pick(entry, "desc"))
        _index(norm, "character", str(cid), "desc", desc.text, desc.refs)

        _normalize_character_detail(str(cid), detail, norm)


def _normalize_character_detail(cid: str, detail: dict, norm: Normalized) -> None:
    if not detail:
        norm.skip(f"角色 {cid}: 没有详情文件,只入库了索引字段")
        return

    info = detail.get("chara_info") or {}
    if info:
        norm.add(
            "character_profiles",
            {
                "character_id": _as_int(cid),
                "birth": _pick(info, "birth"),
                "sex": _pick(info, "sex"),
                "country": _pick(info, "country"),
                "influence": _pick(info, "influence"),
                "talent_name": _pick(info, "talent_name"),
                "talent_doc": _CLEAN(_pick(info, "talent_doc")).text,
                "talent_certification": _CLEAN(_pick(info, "talent_certification")).text,
                "cv_cn": _pick(info, "cv_name_cn"),
                "cv_jp": _pick(info, "cv_name_jp"),
                "cv_ko": _pick(info, "cv_name_ko"),
                "cv_en": _pick(info, "cv_name_en"),
            },
        )
        for f in ("talent_doc", "talent_certification", "info"):
            p = _CLEAN(_pick(info, f))
            _index(norm, "character", cid, f"chara_info.{f}", p.text, p.refs)

    for tag_id, tag in (detail.get("tag") or {}).items():
        norm.add(
            "character_tags",
            {
                "character_id": _as_int(cid),
                "tag_id": str(tag_id),
                "name": _pick(tag, "name"),
                "desc": _CLEAN(_pick(tag, "desc")).text,
                "color": _pick(tag, "color"),
            },
        )

    # stats: {phase: {stage: {life, atk, def}}}
    for phase, stages in (detail.get("stats") or {}).items():
        if not isinstance(stages, dict):
            norm.skip(f"角色 {cid}: stats[{phase}] 不是字典,已跳过")
            continue
        for stage, vals in stages.items():
            if not isinstance(vals, dict):
                continue
            norm.add(
                "character_stats",
                {
                    "character_id": _as_int(cid),
                    "phase": _as_int(phase),
                    "stage": _as_int(stage),
                    "life": vals.get("life"),
                    "atk": vals.get("atk"),
                    "def": vals.get("def"),
                },
            )

    # skill_trees: {tree_id: {node_type, consume, skill: {name, desc}}}
    for tree_id, node in (detail.get("skill_trees") or {}).items():
        skill = node.get("skill") or {}
        p = _parameter_text(norm, "character", cid, f"skill.{tree_id}",
                            skill.get("desc"), skill.get("param"))
        norm.add(
            "character_skills",
            {
                "character_id": _as_int(cid),
                "tree_id": str(tree_id),
                "node_type": _as_int(_pick(node, "node_type")),
                "name": _pick(skill, "name"),
                "desc": p.text,
                "desc_params": _json(p.params),
                "consumes": _json(node.get("consume")),
            },
        )

        simple = _parameter_text(norm, "character", cid, f"skill.{tree_id}.simple",
                                 skill.get("simple_desc"), skill.get("simple_param"))
        norm.add("character_skill_details", {
            "character_id": _as_int(cid), "tree_id": str(tree_id),
            "skill_type": skill.get("type"), "simple_desc": simple.text,
            "parent_nodes": _json(node.get("parent_nodes")),
            "coordinate": _as_int(node.get("coordinate")),
            "unlock_condition": _as_int(node.get("un_lock_condition")),
            "branch_ids": _json(node.get("skill_branch_ids")),
            "damage": _json(skill.get("damage")), "source_payload": _json(node),
        })
        for aid, attr in (skill.get("level") or {}).items():
            norm.add("character_skill_levels", {
                "character_id": _as_int(cid), "tree_id": str(tree_id),
                "attribute_id": str(aid), "name": attr.get("name"),
                "params": _json(attr.get("param")), "format": _json(attr.get("format")),
            })
        for level, costs in (skill.get("consume") or {}).items():
            for idx, cost in enumerate(costs or []):
                norm.add("character_skill_costs", {
                    "character_id": _as_int(cid), "tree_id": str(tree_id),
                    "level": _as_int(level), "idx": idx,
                    "item_key": _as_int(cost.get("key")), "amount": _as_int(cost.get("value")),
                })

    for no, chain in (detail.get("chains") or {}).items():
        p = _parameter_text(norm, "character", cid, f"chain.{no}",
                            chain.get("desc"), chain.get("param"))
        norm.add(
            "character_chains",
            {
                "character_id": _as_int(cid),
                "chain_no": _as_int(no),
                "name": _pick(chain, "name"),
                "desc": p.text,
                "desc_params": _json(p.params),
            },
        )

    for idx, story in enumerate(detail.get("stories") or []):
        p = _CLEAN(_pick(story, "content"))
        norm.add(
            "character_stories",
            {
                "character_id": _as_int(cid),
                "idx": idx,
                "title": _pick(story, "title"),
                "content": p.text,
            },
        )
        _index(norm, "character", cid, f"story.{idx}", p.text, p.refs)

    for idx, voice in enumerate(detail.get("voices") or []):
        p = _CLEAN(_pick(voice, "content"))
        norm.add(
            "character_voices",
            {
                "character_id": _as_int(cid),
                "idx": idx,
                "voice_type": _as_int(_pick(voice, "type")),
                "title": _pick(voice, "title"),
                "content": p.text,
                # 音频资源路径。不是可直接播放的 URL(UE 的 Wwise 事件路径),
                # 但它是这条语音的唯一标识,留着将来做语音功能时有用。
                "voice_asset": _pick(voice, "voice"),
            },
        )
        _index(norm, "character", cid, f"voice.{idx}", p.text, p.refs)

    for phase, mats in (detail.get("ascensions") or {}).items():
        if not isinstance(mats, list):
            norm.skip(f"角色 {cid}: ascensions[{phase}] 不是列表,已跳过")
            continue
        for mat in mats:
            if not isinstance(mat, dict):
                continue
            norm.add(
                "character_ascensions",
                {
                    "character_id": _as_int(cid),
                    "phase": _as_int(phase),
                    "item_key": _as_int(_pick(mat, "key")),
                    "amount": _as_int(_pick(mat, "value")),
                },
            )

    for idx, good in enumerate(detail.get("goods") or []):
        if not isinstance(good, dict):
            continue
        p = _CLEAN(_pick(good, "content"))
        norm.add(
            "character_goods",
            {
                "character_id": _as_int(cid),
                "idx": idx,
                "title": _pick(good, "title"),
                "content": p.text,
            },
        )
        _index(norm, "character", cid, f"good.{idx}", p.text, p.refs)

    # recommend: {weapon: [id...], property: str, param: [...]}
    rec = detail.get("recommend") or {}
    for idx, wid in enumerate(rec.get("weapon") or []):
        norm.add(
            "character_recommend",
            {
                "character_id": _as_int(cid),
                "kind": "weapon",
                "idx": idx,
                "ref_id": _as_int(wid),
                "raw": "",
            },
        )
    if rec.get("property"):
        norm.add(
            "character_recommend",
            {
                "character_id": _as_int(cid),
                "kind": "property",
                "idx": 0,
                "ref_id": None,
                "raw": _json(rec.get("property")),
            },
        )
    if not rec:
        norm.note("部分角色的 recommend 为空 —— 这是源数据的现状,不是解析失败")

    _normalize_character_extras(cid, detail, norm)

    # 嵌套操作说明此前只有 JSON 兜底,现在也进入正文检索。
    for group_id, group in ((detail.get("forte_new") or {}).get("instructions") or {}).items():
        for instruction_id, instruction in (group.get("desc") or {}).items():
            _parameter_text(norm, "character", cid, f"instruction.{group_id}.{instruction_id}",
                            instruction.get("desc"), instruction.get("input_list"))
    for tag_id, tag in (detail.get("tag") or {}).items():
        p = _CLEAN(tag.get("desc"))
        _index(norm, "character", cid, f"tag.{tag_id}", p.text, p.refs)
    norm.add("character_extras", {"character_id": _as_int(cid), "kind": "recommend",
                                  "payload": _json(detail.get("recommend"))})


def _normalize_character_extras(cid: str, detail: dict, norm: Normalized) -> None:
    """按已核对的原始结构归一化料理、模态等角色扩展字段。"""
    cid_int = _as_int(cid)

    # 特色料理:扁平结构,54/64 个角色有
    cook = detail.get("special_cook")
    if isinstance(cook, dict):
        p = _CLEAN(_pick(cook, "desc"))
        norm.add(
            "character_special_cook",
            {
                "character_id": cid_int,
                "cook_id": _as_int(_pick(cook, "id")),
                "name": _pick(cook, "name"),
                "desc": p.text,
                "rarity": _as_int(_pick(cook, "rarity")),
            },
        )
        _index(norm, "character", cid, "special_cook", p.text, p.refs)

    # 弱点/韧性:五个整数,64/64 个角色都有
    weak = detail.get("stats_weakness")
    if isinstance(weak, dict):
        norm.add(
            "character_weakness",
            {
                "character_id": cid_int,
                "weakness_build_up": _as_int(weak.get("weakness_build_up")),
                "weakness_build_up_max": _as_int(weak.get("weakness_build_up_max")),
                "weakness_total_bonus": _as_int(weak.get("weakness_total_bonus")),
                "break_weakness_ratio": _as_int(weak.get("break_weakness_ratio")),
                "weakness_mastery": _as_int(weak.get("weakness_mastery")),
            },
        )

    # 升级经验曲线:长度 100 的整数数组,下标即等级-1
    for i, exp in enumerate(detail.get("level_exp") or []):
        norm.add(
            "character_level_exp",
            {"character_id": cid_int, "level": i + 1, "exp": _as_int(exp)},
        )

    # 皮肤:只取有语义的字段(资源路径不入库)
    for skin_id, sk in (detail.get("skin") or {}).items():
        if not isinstance(sk, dict):
            continue
        p = _CLEAN(_pick(sk, "bg_description"))
        norm.add(
            "character_skins",
            {
                "character_id": cid_int,
                "skin_id": _as_int(_pick(sk, "id")) or _as_int(skin_id),
                "name": _pick(sk, "name"),
                "title_name": _pick(sk, "title_name"),
                "sub_dec_name": _pick(sk, "sub_dec_name"),
                "bg_description": p.text,
                "quality_id": _as_int(_pick(sk, "quality_id")),
                "sort_index": _as_int(_pick(sk, "sort_index")),
            },
        )
        _index(norm, "character", cid, f"skin.{skin_id}", p.text, p.refs)

    # 共鸣模态分支:只有 5/64 个角色有,但结构清晰
    for branch_id, br in (detail.get("skill_branches") or {}).items():
        if not isinstance(br, dict):
            continue
        p = _CLEAN(_pick(br, "desc"))
        norm.add(
            "character_skill_branches",
            {
                "character_id": cid_int,
                "branch_id": _as_int(branch_id),
                "name": _pick(br, "name"),
                "desc": p.text,
            },
        )
        _index(norm, "character", cid, f"branch.{branch_id}", p.text, p.refs)

    # forte_new:features 是扁平列表;instructions 是三层嵌套,存 JSON 兜底
    forte_new = detail.get("forte_new")
    if isinstance(forte_new, dict):
        for i, feat in enumerate(forte_new.get("features") or []):
            p = _CLEAN(feat if isinstance(feat, str) else None)
            if not p.text:
                continue
            norm.add(
                "character_forte_features",
                {"character_id": cid_int, "idx": i, "feature": p.text},
            )
            _index(norm, "character", cid, f"forte.feature.{i}", p.text, p.refs)
        if forte_new.get("instructions"):
            norm.add(
                "character_extras",
                {
                    "character_id": cid_int,
                    "kind": "forte_new.instructions",
                    "payload": _json(forte_new["instructions"]),
                },
            )

    # forte:源里 64/64 个角色都有这个键,但内容普遍是空壳
    # ({skill_input_list:{}, icon:"", desc_list:[]})。有内容时才存。
    forte = detail.get("forte")
    if isinstance(forte, dict) and any(
        forte.get(k) for k in ("skill_input_list", "desc_list", "icon")
    ):
        norm.add(
            "character_extras",
            {"character_id": cid_int, "kind": "forte", "payload": _json(forte)},
        )


# --------------------------------------------------------------------------- 其他实体


def normalize_weapons(data: dict, version: str, norm: Normalized) -> None:
    for wid, w in (data or {}).items():
        p = _CLEAN(_pick(w, "desc"))
        norm.add(
            "weapons",
            {
                "id": _as_int(wid),
                "name": _pick(w, "zh"),
                "en": _pick(w, "en"),
                "rarity": _as_int(_pick(w, "rank")),
                "weapon_type": _as_int(_pick(w, "type")),
                "atk": _as_int(_pick(w, "atk")),
                # 源里就是普通字符串(如 "ATK"),124/133 有值、9 个为 null。
                # 之前用 _json() 包了一层引号,那是错的。
                "sub": _pick(w, "sub") or "",
                "desc": p.text,
                "game_version": version,
            },
        )
        _add_aliases(norm, "weapon", wid, w)
        _index(norm, "weapon", str(wid), "desc", p.text, p.refs)


def normalize_echoes(data: dict, version: str, norm: Normalized) -> None:
    for eid, e in (data or {}).items():
        norm.add("entity_extras", {"entity_type": "echo", "entity_id": str(eid),
                                  "payload": _json({"phantom": e.get("phantom")})})
        norm.add(
            "echoes",
            {
                "id": _as_int(eid),
                "name": _pick(e, "zh"),
                "en": _pick(e, "en"),
                "code": _pick(e, "code"),
                "ranks": _json(e.get("rank")),
                "groups": _json(e.get("group")),
                "intensity": _as_int(_pick(e, "intensity")),
                "game_version": version,
            },
        )
        _add_aliases(norm, "echo", eid, e)


def normalize_sonatas(data: dict, version: str, locale: str, norm: Normalized) -> None:
    """合鸣效果。

    这个文件的结构是全套数据里最绕的:
        name = {zh, en, ja, ko}
        set  = {"2": {zh: {desc, param}, en: {...}}, "5": {...}}
    所以既要做语言选择,也要保留"2 件套 / 5 件套"这个区分 —— 压成一个字符串
    会让"2 件套效果是什么"这类问题答错。
    """
    for sid, s in (data or {}).items():
        if not isinstance(s, dict):
            continue
        name = _localized(s.get("name"), locale)
        pieces = s.get("set") or {}
        texts: list[str] = []

        if isinstance(pieces, dict):
            for piece, by_locale in pieces.items():
                entry = by_locale.get(locale) if isinstance(by_locale, dict) else None
                if not isinstance(entry, dict):
                    # 退一步:有些语言可能缺项,用中文补
                    entry = by_locale.get("zh") if isinstance(by_locale, dict) else None
                if not isinstance(entry, dict):
                    norm.skip(f"合鸣 {sid} 的 {piece} 件套缺少 {locale} 文本")
                    continue
                p = _parameter_text(norm, "sonata", f"{sid}#{piece}", "set",
                                    entry.get("desc"), entry.get("param"))
                texts.append(f"{piece}件套:{p.text}")
                norm.add(
                    "sonata_effects",
                    {
                        "sonata_id": _as_int(sid),
                        "pieces": _as_int(piece),
                        "desc": p.text,
                        "params": _json(entry.get("param")),
                    },
                )

        norm.add(
            "sonatas",
            {
                "id": _as_int(sid),
                "name": name,
                "set_text": "\n".join(texts),
                "game_version": version,
            },
        )
        for lang, alias in (s.get("name") or {}).items():
            if alias:
                norm.add("aliases", {"alias": alias, "entity_type": "sonata",
                                     "entity_id": str(sid), "locale": "", "source": lang})


def normalize_monsters(data: dict, version: str, norm: Normalized) -> None:
    """怪物。字段已确认:`icon / rank / element / echo / zh / en / ja / ko`。

    `echo` 指向 echo.json 的声骸 id —— 这条边让"这个怪掉什么声骸"可以 JOIN 出来。
    """
    known = {"icon", "zh", "en", "ja", "ko", "name", "rank", "element", "echo", "code"}
    for mid, m in (data or {}).items():
        if not isinstance(m, dict):
            continue
        extra = {k: v for k, v in m.items() if k not in known}
        norm.add(
            "monsters",
            {
                "id": _as_int(mid),
                "name": _pick(m, "zh", "name"),
                "en": _pick(m, "en"),
                "rarity": _as_int(_pick(m, "rank")),
                "element": _as_int(_pick(m, "element")),
                "echo_id": _as_int(_pick(m, "echo")),
                "game_version": version,
                "extra_json": _json(extra) if extra else "",
            },
        )
        _add_aliases(norm, "monster", mid, m)


def normalize_items(data: dict, version: str, norm: Normalized) -> None:
    """材料/道具。

    字段已全部确认:`icon / name / rank / tag / tagnum / type`
    (没有 `desc` —— 源数据就不提供材料说明,所以正文检索里没有材料描述)。
    `tag` 是分类标签数组(如 ["通用货币"]),`tagnum` 是与之一一对应的数值。
    """
    observed: set[str] = set()
    for iid, it in (data or {}).items():
        if not isinstance(it, dict):
            continue
        observed.update(it.keys())
        known = {"icon", "zh", "en", "ja", "ko", "name", "rank", "type", "desc", "rarity",
                 "tag", "tagnum"}
        extra = {k: v for k, v in it.items() if k not in known}
        p = _CLEAN(_pick(it, "desc"))
        norm.add(
            "items",
            {
                "id": _as_int(iid),
                "name": _pick(it, "zh", "name"),
                "en": _pick(it, "en"),
                "rarity": _as_int(_pick(it, "rank", "rarity")),
                "item_type": str(_pick(it, "type", default="")),
                "desc": p.text,
                "tags": _json(it.get("tag")),
                "tag_nums": _json(it.get("tagnum")),
                "game_version": version,
                "extra_json": _json(extra) if extra else "",
            },
        )
        _add_aliases(norm, "item", iid, it)
        _index(norm, "item", str(iid), "desc", p.text, p.refs)

    if observed:
        norm.note("item.json 键集合: " + ", ".join(sorted(observed)))


def normalize_hyperlinks(data: dict, norm: Normalized) -> None:
    """655 条术语词条。这是世界观知识的核心原料,也是 <te href> 的解析目标。"""
    for hid, h in (data or {}).items():
        if not isinstance(h, dict):
            continue
        p = _parameter_text(norm, "hyperlink", str(hid), "desc", h.get("desc"), h.get("param"))
        norm.add(
            "hyperlinks",
            {
                "id": _as_int(hid),
                "name": _pick(h, "name"),
                "desc": p.text,
                "params": _json(h.get("param")),
            },
        )
        if _pick(h, "name"):
            norm.add(
                "aliases",
                {
                    "alias": str(_pick(h, "name")).strip(),
                    "entity_type": "hyperlink",
                    "entity_id": str(hid),
                    "locale": "",
                    "source": "hyperlink_name",
                },
            )


# --------------------------------------------------------------------------- 索引


def _add_aliases(norm: Normalized, etype: str, eid, entry: dict) -> None:
    for key in ("zh", "en", "ja", "ko", "name"):
        val = _pick(entry, key)
        if val:
            norm.add(
                "aliases",
                {
                    "alias": str(val).strip(),
                    "entity_type": etype,
                    "entity_id": str(eid),
                    "locale": "",
                    "source": key,
                },
            )


def _index(norm: Normalized, etype: str, eid: str, field_name: str, body: str, refs: list[int]) -> None:
    """写正文字段:进 text_index(短查询 LIKE 兜底)+ 收集实体引用边。"""
    if body and body.strip():
        norm.add(
            "text_index",
            {"entity_type": etype, "entity_id": str(eid), "field": field_name, "body": body},
        )
    for ref in refs or []:
        norm.add(
            "refs",
            {"from_type": etype, "from_id": str(eid), "to_hyperlink_id": int(ref)},
        )
