"""Explicitly curated source excerpts, verified against a saved text snapshot."""
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from contextlib import closing
from urllib.parse import urlsplit

from knowledge.config import DB_PATH
from .store import WikiError

ADAPTER = "curated_guide_v1"
TABLES = {"character": "characters", "weapon": "weapons"}
TERMS = {"Tune Rupture": "震谐", "Fusion Burst": "聚爆", "Mono Fusion": "纯热熔",
         "Res. Liberation": "共鸣解放", "Forte Circuit": "共鸣回路", "Res. Skill": "共鸣技能",
         "Basic Attack": "普攻", "Intro Skill": "变奏技能", "Energy Regen": "共鸣效率"}


def validate_guide(data):
    if not isinstance(data, dict) or data.get("schema") != ADAPTER:
        raise WikiError("不支持的攻略摘录格式")
    for key in ("source_url", "title", "review_patch", "calculation_patch", "updated_at", "captured_at",
                "version_quote", "snapshot_sha256"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise WikiError(f"攻略缺少{key}")
    url = urlsplit(data["source_url"])
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise WikiError("攻略来源须为不含凭证的HTTPS网址")
    if not re.fullmatch(r"[0-9a-f]{64}", data["snapshot_sha256"]):
        raise WikiError("快照SHA256无效")
    for key in ("review_patch", "calculation_patch"):
        if not re.fullmatch(r"\d+(?:\.\d+){1,2}", data[key]):
            raise WikiError("攻略版本格式无效")
    if not isinstance(data.get("entities"), list) or not 1 <= len(data["entities"]) <= 30:
        raise WikiError("攻略须指定1到30个已校验实体")
    for entity in data["entities"]:
        if (not isinstance(entity, dict) or entity.get("type") not in TABLES
                or not str(entity.get("id", "")).isdigit()
                or any(not isinstance(entity.get(k), str) or not entity[k].strip() for k in ("name", "source_name"))):
            raise WikiError("攻略实体映射无效")
    if not isinstance(data.get("excerpts"), list) or not 1 <= len(data["excerpts"]) <= 20:
        raise WikiError("攻略须包含1到20条摘录")
    for excerpt in data["excerpts"]:
        if (not isinstance(excerpt, dict) or any(not isinstance(excerpt.get(k), str)
                or not excerpt[k].strip() for k in ("label", "quote")) or len(excerpt["quote"]) > 6000):
            raise WikiError("攻略摘录为空或过长")
    return data


def import_guide(store, manifest, snapshot, *, source_id, game_version, locale="zh", knowledge_path=None):
    data = validate_guide(json.loads(Path(manifest).read_text(encoding="utf-8")))
    raw_snapshot = Path(snapshot).read_bytes()
    if hashlib.sha256(raw_snapshot).hexdigest() != data["snapshot_sha256"]:
        raise WikiError("攻略快照已变化，请重新审核摘录")
    text = raw_snapshot.decode("utf-8")
    if game_version != data["review_patch"]:
        raise WikiError("不能把来源复核版本改标签用于其他版本")
    if any(quote not in text for quote in [data["version_quote"], data["updated_at"], *[e["quote"] for e in data["excerpts"]]]):
        raise WikiError("攻略摘录无法在原始文本快照定位")
    for label, patch in (("Last review update", data["review_patch"]),
                         ("Last major build/calcs update", data["calculation_patch"])):
        if label + " Patch " + patch not in data["version_quote"]:
            raise WikiError("版本声明没有原文依据")
    path = Path(knowledge_path or DB_PATH).resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        for entity in data["entities"]:
            row = conn.execute(f"SELECT name FROM {TABLES[entity['type']]} WHERE id=?", (entity["id"],)).fetchone()
            if not row or row[0] != entity["name"] or entity["source_name"] not in text:
                raise WikiError("攻略实体名称/ID或原文名称不一致")
    return store.import_source(source_id, game_version=game_version, locale=locale,
        title=data["title"], origin=data["source_url"], media_type="application/json",
        raw=Path(manifest).read_bytes(), metadata={"adapter": ADAPTER, "page_kinds": ["team_guide"],
            "entities": [f"{e['type']}:{e['id']}" for e in data["entities"]], "topics": ["team_guide"],
            "evidence_kind": "community_recommendation", "snapshot_sha256": data["snapshot_sha256"]})


def guide_evidence(source):
    if source["metadata"].get("adapter") != ADAPTER:
        raise WikiError("配队攻略只接受显式审核过的摘录适配器")
    data = validate_guide(json.loads(source["text"]))
    if data["review_patch"] != source["game_version"]:
        raise WikiError("攻略来源版本不匹配")
    mapping = {e["source_name"]: e["name"] for e in data["entities"]}
    context = (f"社区来源观点，复核版本{data['review_patch']}，计算基线{data['calculation_patch']}，"
               f"来源更新日期{data['updated_at']}。未在摘录明确的链数、装备、操作和敌人前提不得推断。")
    for index, excerpt in enumerate(data["excerpts"]):
        path = ["excerpts", index, "quote"]
        anchors = [(path, json.dumps(excerpt["quote"], ensure_ascii=False))]
        for key in ("review_patch", "calculation_patch", "version_quote", "updated_at"):
            anchors.append(([key], json.dumps(data[key], ensure_ascii=False)))
        for entity_index, entity in enumerate(data["entities"]):
            for key in ("name", "source_name", "id"):
                anchors.append((["entities", entity_index, key], json.dumps(entity[key], ensure_ascii=False)))
        yield (excerpt["label"], context + " 已校验实体译名：" + json.dumps(mapping, ensure_ascii=False)
               + " 游戏术语对照（保持原义，不另造译名）：" + json.dumps(TERMS, ensure_ascii=False)
               + "\n原文：" + excerpt["quote"], path, anchors)
