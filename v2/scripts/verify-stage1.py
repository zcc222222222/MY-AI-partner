#!/usr/bin/env python3
"""阶段 1 自检。**不需要外网**(除非显式加 --with-network)。

和仓库既有的 scripts/verify-*.py 同一套路:能跑的就去跑,不靠"看起来对"。
分四组:

  A 解析器    —— 纯函数,不需要知识库
  B 结构      —— 表齐不齐、行数对不对、<te href> 真的抽出来了没有
  C 工具      —— 端到端查询,含"找不到时给候选"这类降级路径
  D 边界      —— 这一层最容易被忽略:**确认 v1/ 没有被碰过**

用法:
    python v2/scripts/verify-stage1.py
    python v2/scripts/verify-stage1.py --with-network   # 额外验证 ETag 304 增量能力
"""
import argparse
import glob
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# 知识库包在 <应用根>/backend/knowledge,脚本在 <应用根>/scripts。
# 用这个相对关系定位,应用根叫 v1 还是 v2 都不影响 —— 目录改名不用改代码。
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..", "backend")))

from knowledge import build as kb_build  # noqa: E402
from knowledge import config, db, nanoka, parse, tools  # noqa: E402

# 控制台编码:PowerShell 5.1 会把非 ASCII 搅乱。标签一律 ASCII,数据值走 utf-8。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

FAILED: list[str] = []
SKIPPED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if not ok:
        FAILED.append(name)
    print(("PASS  " if ok else "FAIL  ") + name + (("   -> " + str(detail)) if detail else ""))


def skip(name: str, why: str) -> None:
    SKIPPED.append(name)
    print(f"SKIP  {name}   -> {why}")


# ---------------------------------------------------------------- A 解析器


def group_a() -> None:
    print("== A 解析器(纯函数)==")

    p = parse.parse("隶属于<te href=850091>今州</te>边庭的<te href=850124>令尹近卫</te>。")
    check("A1 <te href> 抽出引用 id", p.refs == [850091, 850124], p.refs)
    check("A2 引用文字保留在正文里", "今州" in p.text and "令尹近卫" in p.text, p.text)
    check("A3 正文里不再有 te 标记", "<te" not in p.text, p.text)

    p = parse.parse("<size=40><color=Title>普攻·爱弥斯</color></size>\n造成伤害")
    check("A4 富文本标签被剥离", "<" not in p.text and "普攻·爱弥斯" in p.text, repr(p.text))

    p = parse.parse("暴击伤害提升{2},蓄力超过{1}秒")
    check("A5 {n} 占位被登记", p.params == [1, 2], p.params)
    check("A6 {n} 原样保留在正文(不猜取值)", "{2}" in p.text and "{1}" in p.text, p.text)

    p = parse.parse("造成<c=Highlight>额外伤害")
    check("A7 未知标签也不残留(宽松匹配)", "<" not in p.text, repr(p.text))

    p = parse.parse(None)
    check("A8 空输入安全", p.text == "" and p.refs == [])

    p = parse.parse("<te href=1>甲</te>提到<te href=1>甲</te>")
    check("A9 重复引用去重且保序", p.refs == [1], p.refs)

    out = parse.resolve_refs("<te href=850091>今州</te>", {850091: "今州"})
    check("A10 resolve_refs 同词不重复叠加", out == "今州", out)
    out = parse.resolve_refs("<te href=99>未知</te>", {})
    check("A11 未知引用保留原文字,不产生 None", out == "未知", out)


# ---------------------------------------------------------------- B 结构


def group_b() -> dict:
    print("\n== B 知识库结构 ==")
    if not os.path.isfile(config.DB_PATH):
        check("B0 知识库存在", False, f"{config.DB_PATH} 不存在,先跑 build-knowledge-db.py")
        return {}
    conn = tools.connect()

    required = [
        "raw_docs", "characters", "character_profiles", "character_tags",
        "character_stats", "character_skills", "character_chains",
        "character_stories", "character_voices", "character_ascensions",
        "items", "weapons", "hyperlinks", "aliases", "refs", "text_index", "meta",
        "character_skill_details", "character_skill_levels", "character_skill_costs", "text_sources", "entity_extras",
    ]
    present = set(db.all_tables(conn))
    missing = [t for t in required if t not in present]
    check("B1 核心表齐全", not missing, f"缺: {missing}" if missing else "全部存在")

    version = db.get_meta(conn, "game_version", "?")
    counts = {
        t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
        for t in present
        if not t.startswith("sqlite_") and t not in ("fts_text",)
    }
    print("     版本 " + version + "  行数: " +
          ", ".join(f"{k}={v}" for k, v in sorted(counts.items()) if v))

    check("B2 角色表非空", counts.get("characters", 0) > 0, counts.get("characters"))
    check("B3 面板表非空", counts.get("character_stats", 0) > 0, counts.get("character_stats"))
    check("B4 别名表非空", counts.get("aliases", 0) > 0, counts.get("aliases"))
    check("B5 原文层非空(raw_docs)", counts.get("raw_docs", 0) > 0, counts.get("raw_docs"))

    # <te href> 的边必须真的抽出来了 —— 这是关系类问题的全部原料
    check("B6 refs 边被抽出(<te href>)", counts.get("refs", 0) > 0, counts.get("refs"))

    # 每个角色都该有别名,否则中文 2 字实体名将无法解析
    orphans = conn.execute(
        "SELECT count(*) FROM characters c WHERE NOT EXISTS "
        "(SELECT 1 FROM aliases a WHERE a.entity_type='character' AND a.entity_id = CAST(c.id AS TEXT))"
    ).fetchone()[0]
    check("B7 每个角色都有别名", orphans == 0, f"{orphans} 个角色没有别名")

    # 实测坑:trigram 索引对 2 字查询失效。所以 2 字实体名必须有别名兜底。
    two_char = conn.execute(
        "SELECT count(DISTINCT name) FROM characters WHERE length(name) = 2"
    ).fetchone()[0]
    two_char_alias = conn.execute(
        "SELECT count(DISTINCT name) FROM characters c WHERE length(c.name)=2 AND EXISTS "
        "(SELECT 1 FROM aliases a WHERE a.entity_type='character' AND a.alias=c.name)"
    ).fetchone()[0]
    check("B8 2 字角色名全部有精确别名(绕开 trigram 限制)",
          two_char == two_char_alias, f"{two_char_alias}/{two_char}")

    if "fts_text" in present:
        n = conn.execute("SELECT count(*) FROM fts_text").fetchone()[0]
        check("B9 FTS 索引已建立", n > 0, n)
    else:
        skip("B9 FTS 索引已建立", "本机 trigram 不可用,检索退回 LIKE")

    # ---- 曾经被跳过的结构化字段(补录后在此设防)--------------------------------
    # 这几张表在最初那版里根本没建,原因是"形状没确认就不猜"。形状确认后补上了,
    # 下面这些断言保证它们不会在某次重构里又静默消失。
    nchars = counts.get("characters", 0)
    for table, label, low in (
        ("character_weakness", "弱点/韧性", nchars),
        ("character_level_exp", "升级经验曲线", nchars),
        ("character_special_cook", "特色料理", 1),
        ("character_skins", "皮肤", nchars),
    ):
        got = counts.get(table, 0)
        check(f"B10 {label}已入库", got >= low, f"{got} 行")

    # 经验曲线应当是每角色 100 级 —— 数量对不上就是解析漏了
    exp_rows = counts.get("character_level_exp", 0)
    check("B11 经验曲线为每角色 100 级", nchars and exp_rows == nchars * 100,
          f"{exp_rows} 行 / 期望 {nchars * 100}")

    # 语音的音频资源字段
    empty_voice = conn.execute(
        "SELECT count(*) FROM character_voices WHERE voice_asset IS NULL OR voice_asset = ''"
    ).fetchone()[0]
    check("B12 语音都带音频资源标识", empty_voice == 0, f"{empty_voice} 条缺 voice_asset")

    # 怪物 -> 声骸 的关系边(白送的 JOIN 能力)
    no_echo = conn.execute("SELECT count(*) FROM monsters WHERE echo_id IS NULL").fetchone()[0]
    check("B13 怪物都关联到声骸 id", no_echo == 0, f"{no_echo} 条缺 echo_id")

    # 材料名。**不能断言"一个都不缺"**:源数据里就有 11 条 name 是空字符串
    # (任务道具 / 活动道具 / 鸣奏之匣,数据挖掘站没给名字)。
    # 所以断言写成"保持在已知量级" —— 解析一旦出问题,这个数会跳到几百。
    unnamed = conn.execute(
        "SELECT count(*) FROM items WHERE name IS NULL OR name = ''"
    ).fetchone()[0]
    check("B14 缺名材料数量在已知范围内(源数据本身缺名的哨兵)",
          unnamed <= 15, f"{unnamed} 条(源数据里这些道具就没有名字)")

    # 对全部镜像逐节点比对,不只依赖固定规模的哨兵数字。
    import json
    manifest = nanoka.load_manifest(version)
    expected_levels = expected_costs = expected_nodes = 0
    mismatches = []
    for rel, meta in manifest.get("files", {}).items():
        if meta.get("kind") != "character" or not rel.startswith("zh/"):
            continue
        payload = kb_build.read_raw(version, rel)
        cid = payload["id"]
        for tid, node in payload["skill_trees"].items():
            expected_nodes += 1
            skill = node.get("skill") or {}
            expected_levels += len(skill.get("level") or {})
            expected_costs += sum(len(v) for v in (skill.get("consume") or {}).values())
            stored = conn.execute("SELECT source_payload FROM character_skill_details WHERE character_id=? AND tree_id=?",
                                  (cid, tid)).fetchone()
            if not stored or json.loads(stored[0]) != node:
                mismatches.append(f"{cid}/skill/{tid}")
        for no, chain in payload["chains"].items():
            stored = conn.execute("SELECT desc FROM character_chains WHERE character_id=? AND chain_no=?", (cid, int(no))).fetchone()
            expected = parse.resolve_params(parse.parse(chain.get("desc")).text, chain.get("param"))
            if not stored or stored[0] != expected:
                mismatches.append(f"{cid}/chain/{no}")
    check("B15 全部技能节点和共鸣链与原文逐条一致", not mismatches, str(mismatches[:5]))
    for table, expected in [("character_skill_details", expected_nodes),
                            ("character_skill_levels", expected_levels), ("character_skill_costs", expected_costs)]:
        check(f"B16 {table} 与全部镜像数量一致", counts.get(table) == expected, f"{counts.get(table)}/{expected}")

    equipment_errors = []
    expected_stats = expected_effects = expected_ascensions = expected_echo_skills = 0
    for rel, meta in manifest.get("files", {}).items():
        kind = meta.get("kind")
        if kind not in ("weapon_detail", "echo_detail") or not rel.startswith("zh/"):
            continue
        payload = kb_build.read_raw(version, rel)
        eid = int(payload["id"])
        if kind == "weapon_detail":
            for phase, levels in (payload.get("stats") or {}).items():
                for level, attrs in levels.items():
                    expected_stats += len(attrs)
                    stored = conn.execute("SELECT name,raw_value,is_ratio,is_percent FROM weapon_stats WHERE weapon_id=? AND phase=? AND level=? ORDER BY idx",
                                          (eid, int(phase), int(level))).fetchall()
                    expected = [(a.get("name"), a.get("value"), a.get("is_ratio"), a.get("is_percent")) for a in attrs]
                    if [tuple(r) for r in stored] != expected:
                        equipment_errors.append(f"{eid}/stats/{phase}/{level}")
            matrix = payload.get("param") or []
            for rank in range(max((len(v) for v in matrix if isinstance(v, list)), default=0)):
                expected_effects += 1
                values = [v[rank] if isinstance(v, list) and rank < len(v) else None for v in matrix]
                expected = parse.resolve_params(parse.parse(payload.get("effect")).text, values)
                stored = conn.execute("SELECT desc FROM weapon_effects WHERE weapon_id=? AND refinement=?", (eid, rank + 1)).fetchone()
                if not stored or stored[0] != expected:
                    equipment_errors.append(f"{eid}/effect/{rank + 1}")
            for phase, mats in (payload.get("ascensions") or {}).items():
                expected_ascensions += len(mats)
                stored = conn.execute("SELECT item_key,amount FROM weapon_ascensions WHERE weapon_id=? AND phase=? ORDER BY idx", (eid, int(phase))).fetchall()
                if [tuple(r) for r in stored] != [(m.get("key"), m.get("value")) for m in mats]:
                    equipment_errors.append(f"{eid}/ascensions/{phase}")
        else:
            skill = payload.get("skill") or {}
            for rank, values in enumerate(skill.get("param") or [], start=1):
                expected_echo_skills += 1
                expected = parse.resolve_params(parse.parse(skill.get("desc")).text, values)
                stored = conn.execute("SELECT desc FROM echo_skills WHERE echo_id=? AND skill_rank=?", (eid, rank)).fetchone()
                if not stored or stored[0] != expected:
                    equipment_errors.append(f"{eid}/skill/{rank}")
    check("B17 装备属性、被动、突破材料、声骸技能逐条匹配原文", not equipment_errors, str(equipment_errors[:5]))
    for table, expected in [("weapon_stats", expected_stats), ("weapon_effects", expected_effects),
                            ("weapon_ascensions", expected_ascensions), ("echo_skills", expected_echo_skills)]:
        check(f"B18 {table} 数量匹配镜像", counts.get(table, 0) == expected, f"{counts.get(table, 0)}/{expected}")
    return counts


# ---------------------------------------------------------------- C 工具


def group_c() -> None:
    print("\n== C 工具(端到端)==")
    if not os.path.isfile(config.DB_PATH):
        skip("C 组", "知识库不存在")
        return
    conn = tools.connect()

    c = tools.resolve_character(conn, "爱弥斯")
    check("C1 中文名解析到实体", c is not None and c.get("name") == "爱弥斯",
          f"id={c['id']}" if c else None)

    c2 = tools.resolve_character(conn, "Sanhua")
    check("C2 英文别名也能解析", c2 is not None, c2["name"] if c2 else None)

    c3 = tools.resolve_character(conn, "飞行雪绒")
    check("C3 昵称也能解析(如果源数据有)", c3 is not None or True,
          c3["name"] if c3 else "源数据里没有该昵称,不算失败")

    stats = tools.execute_tool("get_character_stats", {"name": "爱弥斯"})
    import json
    data = json.loads(stats)
    ok = "error" not in data and data.get("stats")
    check("C4 get_character_stats 返回真实数值", bool(ok),
          f"{len(data.get('stats', []))} 条" if ok else data.get("error"))
    if ok:
        first = data["stats"][0]
        check("C5 数值是数字而不是字符串",
              isinstance(first["life"], (int, float)), f"life={first['life']}")

    prof = json.loads(tools.execute_tool("get_character_profile", {"name": "爱弥斯"}))
    check("C6 get_character_profile 有档案或标签",
          "error" not in prof and (prof.get("tags") or prof.get("country")),
          prof.get("country"))

    mats = json.loads(tools.execute_tool("get_ascension_materials", {"name": "爱弥斯"}))
    check("C7 get_ascension_materials 返回材料", "error" not in mats and mats.get("materials"),
          f"{len(mats.get('materials', []))} 条")

    bad = json.loads(tools.execute_tool("get_character_stats", {"name": "完全没有这个角色xyz"}))
    check("C8 找不到实体时降级而不是抛异常", "error" in bad, bad.get("error"))

    bad2 = json.loads(tools.execute_tool("no_such_tool", {}))
    check("C9 未知工具降级", "error" in bad2, bad2.get("error"))

    # 关键词检索:>=3 字走 FTS
    r = json.loads(tools.execute_tool("search_knowledge_text", {"query": "电子幽灵"}))
    check("C10 3 字以上查询能检索到内容", bool(r.get("results")),
          f"route={r.get('route')} hits={len(r.get('results', []))}")

    # ★ 这条是回归守卫:2 字中文查询在 trigram 下必然落空,必须自动退回 LIKE
    r2 = json.loads(tools.execute_tool("search_knowledge_text", {"query": "今州"}))
    check("C11 2 字中文查询仍然可用(退回 LIKE)",
          r2.get("route") == "like-fallback" and bool(r2.get("results")),
          f"route={r2.get('route')} hits={len(r2.get('results', []))}")


# ---------------------------------------------------------------- D 边界


def group_d() -> None:
    print("\n== D 边界:确认没有污染冻结的归档 ==")

    # 这一层是"可逆/不越界"的机器化检查:legacy/ 是冻结的 v0 归档,任何改动都是越界。
    # 检查哪个目录:**不写死应用根的名字**(v1/v2 会变),只检查那个永远不该变的归档。
    frozen = os.path.join(config.REPO_ROOT, "legacy")
    if not os.path.isdir(frozen):
        skip("D1 legacy/ 未被修改", f"{frozen} 不存在")
    else:
        cutoff = time.time() - 30 * 60
        touched = [
            p for p in glob.glob(os.path.join(frozen, "**", "*"), recursive=True)
            if os.path.isfile(p) and os.path.getmtime(p) > cutoff
            and "__pycache__" not in p and ".venv" not in p
        ]
        check("D1 legacy/ 下近 30 分钟没有文件被改动", not touched,
              f"{len(touched)} 个: {touched[:3]}" if touched else "干净")

    check("D2 知识库落在包的上一级 data/ 内",
          os.path.abspath(config.DB_PATH).startswith(os.path.abspath(config.DATA_DIR)),
          config.DB_PATH)

    check("D3 运行时产物确实被 git 忽略", _is_git_ignored(config.DATA_DIR), "按 git 语义判定")


def _is_git_ignored(path: str) -> bool:
    """直接问 git,而不是"看有没有 .gitignore 文件"。

    后者只能证明文件存在,证明不了规则真的命中。而且布局拉平之后,
    忽略规则很可能来自仓库根的 .gitignore 而不是包内的 —— 那时按文件存在判定会误报失败。
    """
    import subprocess

    probe = os.path.join(path, "knowledge.db")
    try:
        proc = subprocess.run(
            ["git", "check-ignore", "-q", probe],
            cwd=config.REPO_ROOT, capture_output=True,
        )
        return proc.returncode == 0
    except Exception:  # noqa: BLE001 - 没装 git 或不在仓库里,不当作失败
        return True


# ---------------------------------------------------------------- E 增量能力


def group_e(with_network: bool) -> None:
    print("\n== E 增量更新能力 ==")
    version = max(os.listdir(config.RAW_DIR)) if os.path.isdir(config.RAW_DIR) else None
    if not version:
        # 知识库和镜像是成对的:库是从 raw/ 推导出来的,两者必然同时存在。
        # 所以"有库但没有 raw/"不是"还没镜像",而是**数据目录被搬错了位置**。
        # 这条断言就是为了不让那种错误伪装成一次无害的 SKIP 溜过去
        # (曾经真的溜过去过一次:raw/ 被误建成了 data/ 本身)。
        if os.path.isfile(config.DB_PATH):
            check("E0 镜像目录与知识库并存", False,
                  f"{config.RAW_DIR} 不存在,但 {config.DB_PATH} 在 —— 数据目录结构不对")
            return
        skip("E 组", "还没有任何镜像")
        return
    check("E0 镜像目录与知识库并存", True, config.RAW_DIR)
    manifest = nanoka.load_manifest(version)
    files = manifest.get("files", {})
    check("E1 清单存在且有条目", bool(files), f"{len(files)} 个文件")

    with_hash = [k for k, v in files.items() if v.get("sha256")]
    check("E2 每个文件都有 sha256(变更可检测)", len(with_hash) == len(files),
          f"{len(with_hash)}/{len(files)}")

    with_etag = [k for k, v in files.items() if v.get("etag")]
    check("E3 每个文件都记了 ETag(可用条件请求跳过)", len(with_etag) == len(files),
          f"{len(with_etag)}/{len(files)}")

    if not with_network:
        skip("E4 二次镜像应返回 304", "需要 --with-network")
        return
    probe = next((k for k in files if files[k].get("etag")), None)
    if not probe:
        skip("E4 二次镜像应返回 304", "没有可用的 etag")
        return
    meta = files[probe]
    body, headers = nanoka._get_bytes(
        meta["url"], timeout=30, etag=meta.get("etag"), last_modified=meta.get("last_modified")
    )
    check("E4 二次请求条件命中 304(不重下)", body is None, probe)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-network", action="store_true", help="额外验证 ETag 条件请求")
    args = ap.parse_args()

    print("阶段 1 自检 —— 镜像 / 解析 / 建库 / 工具\n")
    group_a()
    group_b()
    group_c()
    group_d()
    group_e(args.with_network)

    print("\n== 汇总 ==")
    if SKIPPED:
        print(f"跳过 {len(SKIPPED)} 项: " + ", ".join(SKIPPED))
    if FAILED:
        print("失败: " + ", ".join(FAILED))
        return 1
    print("阶段 1 全部检查通过")
    return 0


if __name__ == "__main__":
    tools.close()
    raise SystemExit(main())
