#!/usr/bin/env python3
"""把已镜像的原始数据建成 SQLite 知识库。**完全离线。**

用法:
    python v2/scripts/build-knowledge-db.py
    python v2/scripts/build-knowledge-db.py --version 3.7 --locale zh
    python v2/scripts/build-knowledge-db.py --keep     # 不删旧表,追加(默认是整库重建)

为什么默认整库重建:知识库不含用户数据,它的正确性来自"能从 raw/ 重新推导出来"。
原地增量更新会让"表里的状态"和"raw/ 的真相"逐渐漂移,而这种漂移是不可见的。
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# 知识库包在 <应用根>/backend/knowledge,脚本在 <应用根>/scripts。
# 用这个相对关系定位,应用根叫 v1 还是 v2 都不影响 —— 目录改名不用改代码。
sys.path.insert(0, os.path.normpath(os.path.join(HERE, "..", "backend")))

from knowledge import build, config  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", default=None, help="默认取 raw/ 下最新的版本")
    ap.add_argument("--locale", default=config.DEFAULT_LOCALE)
    ap.add_argument("--db", default=config.DB_PATH, help="知识库文件路径")
    ap.add_argument("--keep", action="store_true", help="保留已有表(默认整库重建)")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出报告,便于脚本消费")
    args = ap.parse_args()

    try:
        report = build.build(
            version=args.version, locale=args.locale, db_path=args.db, reset=not args.keep
        )
    except RuntimeError as exc:
        print(f"[fail] {exc}")
        return 2

    if args.json:
        print(json.dumps(
            {
                "version": report.version,
                "locale": report.locale,
                "counts": report.counts,
                "capability": report.capability,
                "notes": report.notes,
                "skipped": report.skipped,
                "warnings": report.warnings,
                "fields": report.fields,
            },
            ensure_ascii=False, indent=2))
        return 0

    print(f"版本    : {report.version}  语言: {report.locale}")
    print(f"数据库  : {args.db}")
    print(f"SQLite  : {report.capability.get('sqlite_version')}  "
          f"FTS5={report.capability.get('fts5')}  trigram={report.capability.get('trigram')}")
    print("\n=== 写入行数 ===")
    for table, n in sorted(report.counts.items(), key=lambda kv: -kv[1]):
        print(f"  {table:<24} {n:>7,}")

    if report.warnings:
        print("\n=== 警告(需要处理)===")
        for w in report.warnings:
            print(f"  ! {w}")
    if report.notes:
        print("\n=== 观察(信息性)===")
        for n in report.notes:
            print(f"  - {n}")
    if report.skipped:
        print("\n=== 跳过(不影响其他数据)===")
        for s in report.skipped[:10]:
            print(f"  - {s}")
        if len(report.skipped) > 10:
            print(f"  ... 另有 {len(report.skipped) - 10} 条")

    print("\n下一步: python v2/scripts/verify-stage1.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
