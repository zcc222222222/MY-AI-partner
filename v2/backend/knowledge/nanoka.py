"""nanoka 镜像适配器。

这不是爬虫。源站把数据放在**版本号命名的静态 JSON** 上:

    https://static.nanoka.cc/ww/{version}/character.json
    https://static.nanoka.cc/ww/{version}/{locale}/character/{id}.json

版本号在 URL 里 —— 这正是增量更新的基础:新版本 = 新目录,老版本原地不动,
可以逐文件比对、也可以保留历史版本回答"3.6 的时候是怎样的"。

三条设计约束:

1. **源站会消失。** 同品类的 hakush.in 域名已经不解析了。所以镜像必须完整落盘,
   而不是每次查询都去联网。
2. **可续跑。** 用 ETag/Last-Modified 做条件请求:没变的文件返回 304,不下载、不算 sha256。
   重跑一次的成本是几十个 304,而不是 10MB 流量。
3. **原子写。** 半截文件比没有文件更糟 —— 它会被下游当成"已经镜像过了"。
   一律先写 .tmp 再 os.replace。

适配器模式:源站死了只需要换掉这个文件,下游(normalize/build/tools)完全不受影响。
"""
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

from . import config

VERSION_RE = re.compile(r"static\.nanoka\.cc/ww/([\d.]+)/")
_last_request = 0.0
DETAIL_KINDS = ("character", "weapon", "echo")

# 文件名 -> kind 必须显式映射,不能用 name.split(".")[0] 推导:
# "character.json"(索引)和 "character/{id}.json"(详情)会推出同一个 kind,
# 下游就分不清"这是角色名册"还是"这是某个角色的详情"了。
ROOT_FILE_KINDS = {
    "character.json": "roster",
    "echo.json": "echo",
    "weapon.json": "weapon",
    "sonata.json": "sonata",
    "monster.json": "monster",
}
LOCALE_FILE_KINDS = {
    "item.json": "item",
    "hyperlink.json": "hyperlink",
}


@dataclass
class FilePlan:
    """一个待镜像的文件。kind 用于下游归一化时分派。"""

    kind: str  # roster / weapon / echo / sonata / monster / item / hyperlink / character
    doc_id: str  # 文件名或角色 id
    url: str
    rel_path: str  # 相对 raw/{version}/ 的路径,与上游布局一致


@dataclass
class MirrorReport:
    version: str = ""
    locale: str = ""
    fetched: list = field(default_factory=list)
    not_modified: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    bytes_written: int = 0

    def summary(self) -> str:
        return (
            f"version={self.version} locale={self.locale} "
            f"fetched={len(self.fetched)} not_modified={len(self.not_modified)} "
            f"failed={len(self.failed)} skipped={len(self.skipped)} bytes={self.bytes_written:,}"
        )


# --------------------------------------------------------------------------- 规划


def plan_files(version: str, locale: str, character_ids: list[str] | None = None) -> list[FilePlan]:
    """列出一次完整镜像需要哪些文件。

    语言无关的索引文件放在版本根目录;其余在 {locale}/ 下 —— 与上游布局一致,
    这样"这个文件从哪来"永远不需要猜。
    """
    base = f"{config.SOURCE_BASE}/{version}"
    plans: list[FilePlan] = []

    for name in config.ROOT_FILES:
        plans.append(
            FilePlan(
                kind=ROOT_FILE_KINDS.get(name, name.split(".")[0]),
                doc_id=name,
                url=f"{base}/{name}",
                rel_path=name,
            )
        )

    for name in config.LOCALE_FILES:
        plans.append(
            FilePlan(
                kind=LOCALE_FILE_KINDS.get(name, name.split(".")[0]),
                doc_id=name,
                url=f"{base}/{locale}/{name}",
                rel_path=f"{locale}/{name}",
            )
        )

    for cid in character_ids or []:
        rel = config.CHARACTER_DETAIL.format(id=cid)
        plans.append(
            FilePlan(
                kind="character",
                doc_id=str(cid),
                url=f"{base}/{locale}/{rel}",
                rel_path=f"{locale}/{rel}",
            )
        )
    return plans


def detect_version(timeout: float = 30.0) -> str:
    """从站点首页的 data-url 里读出当前版本号。

    源站没有 version.json 之类的索引(已探测,404)。首页 HTML 里每个懒加载数据
    都带 data-url="https://static.nanoka.cc/ww/3.7/..." —— 这是最可靠的口径,
    比维护一份会过期的本地游标好。
    """
    body, _headers = _get_bytes(config.SITE_ROOT, timeout=timeout)
    text = (body or b"").decode("utf-8", "replace")
    match = VERSION_RE.search(text)
    if not match:
        raise RuntimeError(
            "无法从站点首页解析出数据版本号。上游布局可能变了,"
            "请检查 https://ww.nanoka.cc/ 首页里是否还有 static.nanoka.cc/ww/<版本>/ 这样的 URL。"
        )
    return match.group(1)


def plan_details(version: str, locale: str, kind: str, ids: list[str]) -> list[FilePlan]:
    if kind not in DETAIL_KINDS:
        raise ValueError(f"不支持的详情类型: {kind}")
    if not re.fullmatch(r"\d+(?:\.\d+)*", version) or locale not in ("zh", "en", "ja", "ko"):
        raise ValueError("版本或语言不合法")
    if any(not str(eid).isdigit() for eid in ids):
        raise ValueError("实体 id 必须是数字")
    return [FilePlan(kind if kind == "character" else kind + "_detail", str(eid),
                     f"{config.SOURCE_BASE}/{version}/{locale}/{kind}/{eid}.json",
                     f"{locale}/{kind}/{eid}.json") for eid in ids]


# --------------------------------------------------------------------------- 抓取


def _get_bytes(url: str, *, timeout: float, etag: str | None = None,
               last_modified: str | None = None,
               request_interval: float = config.DEFAULT_DELAY_SECONDS) -> tuple[bytes | None, dict]:
    """发一次请求。返回 (body, headers);body 为 None 表示 304 未修改。

    headers 的键**统一小写**:http.client 的 header 对象是大小写不敏感的,
    但一旦 dict() 成普通字典就丢了这个性质。源站(Cloudflare)发的正是小写的
    ``etag`` / ``last-modified`` —— 用 ``headers["ETag"]`` 取会静默拿到 None,
    于是条件请求永远不生效,每次都全量重下。这个坑是靠"二次镜像应有 304"这条
    断言抓出来的。
    """
    headers = {"User-Agent": config.USER_AGENT, "Accept": "application/json,*/*"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    global _last_request
    interval = max(config.MIN_DELAY_SECONDS, request_interval)
    wait = interval - (time.monotonic() - _last_request)
    _pause(max(0, wait))
    _last_request = time.monotonic()
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return None, {}
        raise


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_atomic(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- 清单


def manifest_path(version: str) -> str:
    return os.path.join(config.RAW_DIR, version, "_manifest.json")


def load_manifest(version: str) -> dict:
    path = manifest_path(version)
    if not os.path.isfile(path):
        return {"game_version": version, "files": {}}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_manifest(version: str, manifest: dict) -> None:
    manifest["updated_at"] = _now()
    _write_atomic(manifest_path(version), json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))


def local_path(version: str, rel_path: str) -> str:
    return os.path.join(config.RAW_DIR, version, rel_path.replace("/", os.sep))


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# --------------------------------------------------------------------------- 主流程


def mirror(
    *,
    version: str | None = None,
    locale: str = config.DEFAULT_LOCALE,
    delay: float = config.DEFAULT_DELAY_SECONDS,
    force: bool = False,
    limit: int | None = None,
    with_details: bool = True,
    timeout: float = 45.0,
    on_progress=None,
    detail_kinds: tuple[str, ...] = DETAIL_KINDS,
    skip_existing: bool = False,
) -> MirrorReport:
    """把指定版本完整镜像到 data/raw/{version}/。

    limit 用于先跑通链路 —— 调试时没必要等 64 个角色文件。
    force=True 时忽略 ETag,强制重下(上游改了内容但没换 ETag 的极端情况)。
    """
    if delay < config.MIN_DELAY_SECONDS:
        raise ValueError(f"请求间隔不能小于 {config.MIN_DELAY_SECONDS} 秒")
    if limit is not None and limit < 0:
        raise ValueError("limit 不能为负数")
    if any(kind not in DETAIL_KINDS for kind in detail_kinds):
        raise ValueError("未知的详情类型")
    config.ensure_dirs()
    report = MirrorReport(locale=locale)

    if version is None:
        version = detect_version(timeout=timeout)
    report.version = version
    manifest = load_manifest(version)
    files_meta: dict = manifest.setdefault("files", {})
    manifest["locale"] = locale

    completed = False

    def checkpoint():
        manifest["last_mirror"] = {
            "locale": locale, "detail_kinds": list(detail_kinds),
            "delay_seconds": delay, "traversal_completed": completed,
            "all_requested_succeeded": completed and not report.failed,
            "fetched": len(report.fetched), "not_modified": len(report.not_modified),
            "skipped": len(report.skipped), "failed": report.failed,
        }
        save_manifest(version, manifest)
    try:
        _run_plans(plan_files(version, locale), version, files_meta, report, delay, force,
                   timeout, on_progress, checkpoint, skip_existing)
        if with_details:
            for kind in detail_kinds:
                with open(local_path(version, f"{kind}.json"), encoding="utf-8") as fh:
                    ids = list(json.load(fh))
                if limit is not None:
                    ids = ids[:limit]
                _run_plans(plan_details(version, locale, kind, ids), version, files_meta,
                           report, delay, force, timeout, on_progress, checkpoint, skip_existing)
        completed = True
    finally:
        checkpoint()
    return report


def _run_plans(plans, version, files_meta, report, delay, force, timeout, on_progress,
               checkpoint=None, skip_existing=False) -> None:
    consecutive_failures = 0
    for plan in plans:
        meta = files_meta.get(plan.rel_path, {})
        # 清单还在、原文被删除时不能发送条件请求,否则 304 永远补不回文件。
        use_cache = not force and os.path.isfile(local_path(version, plan.rel_path))
        if use_cache and meta.get("sha256"):
            with open(local_path(version, plan.rel_path), "rb") as fh:
                use_cache = _sha256(fh.read()) == meta["sha256"]
        etag = meta.get("etag") if use_cache else None
        last_modified = meta.get("last_modified") if use_cache else None
        if skip_existing and use_cache and meta.get("sha256"):
            report.skipped.append(plan.rel_path)
            continue

        try:
            for attempt in range(3):
                try:
                    body, headers = _get_bytes(plan.url, timeout=timeout, etag=etag,
                                               last_modified=last_modified, request_interval=delay)
                    break
                except urllib.error.HTTPError as exc:
                    if exc.code not in (429, 503) or attempt == 2:
                        raise
                    retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                    try:
                        seconds = float(retry_after)
                    except ValueError:
                        try:
                            seconds = parsedate_to_datetime(retry_after).timestamp() - time.time()
                        except (ValueError, TypeError, OverflowError):
                            seconds = 0
                    backoff = max(5 * 2 ** attempt, seconds, delay)
                    if on_progress:
                        on_progress("WAIT", f"HTTP {exc.code}, {backoff:.1f}s")
                    _pause(backoff)
            if body is not None:
                decoded = json.loads(body)
                if not isinstance(decoded, dict) or not decoded:
                    raise ValueError("响应不是非空 JSON 对象")
                if plan.kind in ("character", "weapon_detail", "echo_detail") and str(decoded.get("id")) != plan.doc_id:
                    raise ValueError("详情 id 与请求 id 不一致")
            elif not use_cache:
                raise ValueError("本地无有效原文,上游却返回 304")
        except Exception as exc:  # noqa: BLE001 - 单个文件失败不该中断整次镜像
            consecutive_failures += 1
            report.failed.append((plan.rel_path, f"{type(exc).__name__}: {exc}"))
            if on_progress:
                on_progress("FAIL", plan.rel_path)
            if (isinstance(exc, urllib.error.HTTPError) and exc.code in (403, 429, 503)) or consecutive_failures >= 3:
                raise RuntimeError("上游拒绝访问/限流或连续失败,已停止采集并保存进度") from exc
            _pause(delay)
            continue

        consecutive_failures = 0

        if body is None:
            report.not_modified.append(plan.rel_path)
            if on_progress:
                on_progress("304 ", plan.rel_path)
            # 304 也要限速:否则"重跑一次确认没变化"会变成一次无间隔的请求风暴。
            _pause(delay)
            continue

        _write_atomic(local_path(version, plan.rel_path), body)
        files_meta[plan.rel_path] = {
            "kind": plan.kind,
            "doc_id": plan.doc_id,
            "url": plan.url,
            "etag": headers.get("etag", ""),
            "last_modified": headers.get("last-modified", ""),
            "sha256": _sha256(body),
            "bytes": len(body),
            "fetched_at": _now(),
        }
        report.fetched.append(plan.rel_path)
        report.bytes_written += len(body)
        if checkpoint:
            checkpoint()
        if on_progress:
            on_progress("GET ", f"{plan.rel_path} ({len(body):,} B)")
        _pause(delay)


def _pause(delay: float) -> None:
    if delay and delay > 0:
        time.sleep(delay)
