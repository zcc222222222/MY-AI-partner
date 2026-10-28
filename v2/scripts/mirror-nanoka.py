#!/usr/bin/env python3
"""把 nanoka 的某个版本完整镜像到 <包的上一级>/data/raw/(当前是 v2/data/raw/)。

用法:
    python v2/scripts/mirror-nanoka.py                    # 自动探测版本,全文镜像
    python v2/scripts/mirror-nanoka.py --limit 5          # 只抓 5 个角色,先跑通链路
    python v2/scripts/mirror-nanoka.py --version 3.7      # 指定版本
    python v2/scripts/mirror-nanoka.py --dry-run          # 只打印计划,不下载
    python v2/scripts/mirror-nanoka.py --locale en        # 换语言

可重复执行:已镜像且未修改的文件返回 304,不会重新下载。
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# 知识库包在 <应用根>/backend/knowledge,脚本在 <应用根>/scripts。
# 用这个相对关系定位,应用根叫 v1 还是 v2 都不影响 —— 目录改名不用改代码。
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..", "backend")))

from knowledge import config, nanoka  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", default=None, help="游戏数据版本,默认自动探测")
    ap.add_argument("--locale", default=config.DEFAULT_LOCALE, help="语言目录:zh/en/ja/ko")
    ap.add_argument("--delay", type=float, default=config.DEFAULT_DELAY_SECONDS,
                    help=f"请求间隔秒数,默认 {config.DEFAULT_DELAY_SECONDS}")
    ap.add_argument("--force", action="store_true", help="忽略 ETag,强制重下")
    ap.add_argument("--limit", type=int, default=None, help="每种实体只抓前 N 个详情(调试用)")
    ap.add_argument("--no-details", action="store_true", help="只抓索引文件,不抓详情")
    ap.add_argument("--kinds", nargs="+", choices=nanoka.DETAIL_KINDS, default=list(nanoka.DETAIL_KINDS),
                    help="详情类型,默认角色/武器/声骸")
    ap.add_argument("--skip-existing", action="store_true", help="校验本地 SHA-256 后跳过已下载文件,用于断点续跑")
    ap.add_argument("--timeout", type=float, default=20, help="单次请求超时秒数,默认 20")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划,不发起下载")
    args = ap.parse_args()
    if args.delay < config.MIN_DELAY_SECONDS or args.timeout <= 0 or (args.limit is not None and args.limit < 0):
        ap.error("delay 不得小于 1 秒,timeout 必须大于 0,limit 不能为负数")
    if args.dry_run and not args.version:
        ap.error("离线 dry-run 请显式指定 --version,避免自动探测产生网络请求")

    try:
        version = args.version or nanoka.detect_version()
    except Exception as exc:  # noqa: BLE001
        print(f"[fail] 版本探测失败: {exc}")
        return 2

    plans = nanoka.plan_files(version, args.locale)
    print(f"版本    : {version}")
    print(f"语言    : {args.locale}")
    print(f"计划文件: {len(plans)} 个索引文件" + ("" if args.no_details else f" + {', '.join(args.kinds)} 详情"))
    print(f"采集策略: 单线程,请求后等待 {args.delay} 秒;429/503 退避;逐文件保存进度")
    print(f"输出目录: {os.path.join(config.RAW_DIR, version)}")

    if args.dry_run:
        for p in plans:
            print(f"  - {p.rel_path}")
        if not args.no_details:
            for kind in args.kinds:
                path = nanoka.local_path(version, f"{kind}.json")
                if os.path.isfile(path):
                    import json
                    with open(path, encoding="utf-8") as fh:
                        ids = list(json.load(fh))
                    for p in nanoka.plan_details(version, args.locale, kind, ids[:args.limit] if args.limit is not None else ids):
                        print(f"  - {p.rel_path}")
        print("\n(dry run,未发起任何请求)")
        return 0

    def progress(tag: str, detail: str) -> None:
        print(f"  [{tag}] {detail}")

    try:
        report = nanoka.mirror(
            version=version,
            locale=args.locale,
            delay=args.delay,
            force=args.force,
            limit=args.limit,
            with_details=not args.no_details,
            on_progress=progress,
            detail_kinds=tuple(args.kinds),
            skip_existing=args.skip_existing,
            timeout=args.timeout,
        )
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"[fail] {exc}")
        print(f"进度已保存: {nanoka.manifest_path(version)}")
        return 2

    print("\n=== 镜像结果 ===")
    print(f"  {report.summary()}")
    if report.failed:
        print(f"  失败 {len(report.failed)} 个:")
        for rel, err in report.failed[:10]:
            print(f"    - {rel}: {err}")
    print(f"  清单: {nanoka.manifest_path(version)}")
    print("\n下一步: python v2/scripts/build-knowledge-db.py")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
