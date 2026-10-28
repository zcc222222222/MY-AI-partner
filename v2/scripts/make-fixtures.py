#!/usr/bin/env python3
"""从真实镜像里裁出一份小样本,放进 v2/tests/fixtures/。

为什么需要它:test_stage1.py 要能在**没有网络、没有完整镜像**的机器上跑(将来的 CI)。
但样本又必须是**真实数据的真实形状** —— 手写的假数据会在源站改结构时依然"通过",
那就失去了守门的意义。

裁剪原则:保留所有键的形状,只截断数组长度和条目数量。所以样本小,但走的代码路径
和真实构建完全一致。

用法:
    python v2/scripts/make-fixtures.py                 # 默认取最新镜像版本
    python v2/scripts/make-fixtures.py --version 3.7
    python v2/scripts/make-fixtures.py --keep-characters 1210,1102
"""
import argparse
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# 知识库包在 <应用根>/backend/knowledge,脚本在 <应用根>/scripts。
# 用这个相对关系定位,应用根叫 v1 还是 v2 都不影响 —— 目录改名不用改代码。
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..", "backend")))

from knowledge import config, nanoka  # noqa: E402

# 样本放在 <应用根>/tests/fixtures/raw —— 与测试代码同级,改名不影响。
APP_ROOT = os.path.dirname(HERE)
FIXTURE_ROOT = os.path.join(APP_ROOT, "tests", "fixtures", "raw")

# 默认样本:爱弥斯(主角,5 星)+ 散华(2 字名,专门守 trigram 那个坑)
DEFAULT_CHARACTERS = ["1210", "1102"]

# 各数组字段保留多少条。取值只要够走通归一化分支即可。
TRIM_ARRAYS = {
    "stories": 2,
    "voices": 3,
    "chains": 2,
    "goods": 1,
    "level_exp": 3,  # 源数据里 100 条升级经验曲线,形状对就行
}
TRIM_MAPS = {
    "stats": 2,
    "skill_trees": 3,
    "ascensions": 2,
    "skin": 1,
}


def trim_character(detail: dict) -> dict:
    out = json.loads(json.dumps(detail, ensure_ascii=False))

    def cut_key(key: str, n: int) -> None:
        v = out.get(key)
        if isinstance(v, list):
            out[key] = v[:n]
        elif isinstance(v, dict):
            out[key] = {k: v[k] for k in list(v)[:n]}

    for key, n in TRIM_ARRAYS.items():
        cut_key(key, n)
    for key, n in TRIM_MAPS.items():
        cut_key(key, n)

    # 只留前两个 tag,避免样本里一堆无关标签
    if isinstance(out.get("tag"), dict):
        out["tag"] = {k: out["tag"][k] for k in list(out["tag"])[:2]}
    return out


def collect_refs(obj) -> set[int]:
    """从任意嵌套结构里捞出所有 12/80/85 等形态的引用 id 与素材 id。

    用正则扫"看起来像引用 id"的数字太宽松,所以这里只认结构化线索:
    <te href=N> 的 N、以及 ascensions 里的 item key。
    """
    import re

    te = re.compile(r"<te\s+href=(\d+)")
    found: set[int] = set()

    def walk(node, key_hint=""):
        if isinstance(node, str):
            found.update(int(m) for m in te.findall(node))
        elif isinstance(node, dict):
            if key_hint == "ascensions" or ("key" in node and "value" in node):
                k = node.get("key")
                if isinstance(k, int):
                    found.add(k)
            for k, v in node.items():
                walk(v, k if not isinstance(v, (dict, list)) else key_hint)
        elif isinstance(node, list):
            for v in node:
                walk(v, key_hint)

    walk(obj)
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", default=None)
    ap.add_argument("--locale", default=config.DEFAULT_LOCALE)
    ap.add_argument("--keep-characters", default=",".join(DEFAULT_CHARACTERS))
    ap.add_argument("--max-hyperlinks", type=int, default=12)
    ap.add_argument("--max-items", type=int, default=12)
    ap.add_argument("--keep-weapons", default="21020015,21010011")
    ap.add_argument("--keep-echoes", default="390070051")
    args = ap.parse_args()

    version = args.version
    if version is None:
        versions = sorted(os.listdir(config.RAW_DIR)) if os.path.isdir(config.RAW_DIR) else []
        versions = [v for v in versions if os.path.isdir(os.path.join(config.RAW_DIR, v))]
        if not versions:
            print("[fail] 没有镜像可用,先跑 scripts/mirror-nanoka.py")
            return 2
        version = versions[-1]

    src = os.path.join(config.RAW_DIR, version)
    if not os.path.isdir(src):
        print(f"[fail] 找不到镜像目录 {src}")
        return 2

    dst = os.path.join(FIXTURE_ROOT, version)
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    os.makedirs(dst, exist_ok=True)

    characters = [c.strip() for c in args.keep_characters.split(",") if c.strip()]
    files: dict[str, dict] = {}

    def write_doc(rel: str, payload, kind: str, doc_id: str) -> None:
        path = os.path.join(dst, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
        files[rel] = {
            "kind": kind,
            "doc_id": doc_id,
            "url": f"{config.SOURCE_BASE}/{version}/{rel}",
            "etag": "",
            "last_modified": "",
            "sha256": "",
            "bytes": os.path.getsize(path),
            "fetched_at": "fixture",
        }

    # 名册:只留样本角色
    roster_full = json.load(open(os.path.join(src, "character.json"), encoding="utf-8"))
    roster = {cid: roster_full[cid] for cid in characters if cid in roster_full}
    if not roster:
        print(f"[fail] 名册里没有这些角色: {characters}")
        return 2
    write_doc("character.json", roster, "roster", "character.json")

    # 角色详情 + 收集引用
    refs: set[int] = set()
    for cid in roster:
        p = os.path.join(src, args.locale, "character", f"{cid}.json")
        if not os.path.isfile(p):
            print(f"  [warn] 缺 {cid} 的详情,跳过")
            continue
        detail = trim_character(json.load(open(p, encoding="utf-8")))
        refs |= collect_refs(detail)
        write_doc(f"{args.locale}/character/{cid}.json", detail, "character", cid)

    # 装备样本从真实详情裁剪,保留参数矩阵与突破前后相同等级。
    for kind, chosen in (("weapon", args.keep_weapons), ("echo", args.keep_echoes)):
        index = json.load(open(os.path.join(src, f"{kind}.json"), encoding="utf-8"))
        selected = {}
        for eid in (s.strip() for s in chosen.split(",") if s.strip()):
            path = os.path.join(src, args.locale, kind, f"{eid}.json")
            if eid not in index or not os.path.isfile(path):
                print(f"  [warn] 缺少 {kind}/{eid} 详情,跳过样本")
                continue
            with open(path, encoding="utf-8") as fh:
                detail = json.load(fh)
            if kind == "weapon":
                detail["stats"] = {phase: {level: attrs for level, attrs in levels.items()
                                           if level in ("1", "20", "90")}
                                   for phase, levels in detail.get("stats", {}).items()
                                   if phase in ("0", "1", "6")}
            selected[eid] = index[eid]
            refs |= collect_refs(detail)
            write_doc(f"{args.locale}/{kind}/{eid}.json", detail, kind + "_detail", eid)
        if selected:
            write_doc(f"{kind}.json", selected, kind, f"{kind}.json")

    # 词条:被引用到的 + 头部若干(保证样本里既有"能解析的引用"也有"普通词条")
    hl_full = json.load(open(os.path.join(src, args.locale, "hyperlink.json"), encoding="utf-8"))
    hl_ids = [k for k in hl_full if int(k) in refs]
    for k in list(hl_full):
        if len(hl_ids) >= args.max_hyperlinks:
            break
        if k not in hl_ids:
            hl_ids.append(k)
    hyperlinks = {k: hl_full[k] for k in hl_ids}
    write_doc(f"{args.locale}/hyperlink.json", hyperlinks, "hyperlink", "hyperlink.json")

    # 材料:突破用到的 + 头部若干
    item_full = json.load(open(os.path.join(src, args.locale, "item.json"), encoding="utf-8"))
    item_ids = [k for k in item_full if int(k) in refs]
    for k in list(item_full):
        if len(item_ids) >= args.max_items:
            break
        if k not in item_ids:
            item_ids.append(k)
    items = {k: item_full[k] for k in item_ids}
    write_doc(f"{args.locale}/item.json", items, "item", "item.json")

    manifest = {"game_version": version, "locale": args.locale, "files": files,
                "updated_at": "fixture", "note": "由 scripts/make-fixtures.py 裁剪生成"}
    with open(os.path.join(dst, "_manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    total = sum(f["bytes"] for f in files.values())
    print(f"样本目录: {dst}")
    print(f"  文件 {len(files)} 个,共 {total:,} B ({total/1024:.1f} KB)")
    for rel in sorted(files):
        print(f"    {rel}  ({files[rel]['bytes']:,} B)")
    print(f"  覆盖引用 id {len(refs)} 个,词条 {len(hyperlinks)} 条,材料 {len(items)} 条")
    print("\n注意:样本是第三人数据的裁剪,只用于测试代码契约,不作为知识库内容分发。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
