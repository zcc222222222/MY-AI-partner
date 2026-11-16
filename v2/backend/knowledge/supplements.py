"""Add one newly released character from verified local mirrors without relabeling older data."""
import hashlib
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import tempfile

from . import build, config, db, equipment, nanoka, normalize


def add_character(*, version, character_id, expected_name, db_path=config.DB_PATH, locale='zh'):
    cid = str(character_id)
    if not re.fullmatch(r'\d+\.\d+(?:\.\d+)?', version) or not cid.isdecimal() or locale != 'zh' or not expected_name:
        raise ValueError('须指定明确版本、角色ID及中文名称')
    target_path = Path(db_path).resolve()
    if not target_path.is_file():
        raise ValueError('须先准备已有结构化知识库')
    manifest = nanoka.load_manifest(version)
    used = {}

    def read(rel, kind):
        meta = manifest.get('files', {}).get(rel, {})
        path = Path(nanoka.local_path(version, rel))
        if meta.get('kind') != kind or not path.is_file():
            raise ValueError('缺少已镜像的原文：' + rel)
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != meta.get('sha256'):
            raise ValueError('镜像校验失败：' + rel)
        used[rel] = meta
        return json.loads(raw)

    roster = read('character.json', 'roster')
    detail_path = f'{locale}/character/{cid}.json'
    detail = read(detail_path, 'character')
    if (str(detail.get('id')) != cid or detail.get('name') != expected_name
            or roster.get(cid, {}).get(locale) != expected_name):
        raise ValueError('角色ID、详情与名册名称不一致，未导入')
    required = ('stats', 'skill_trees', 'chains', 'ascensions')
    if any(not detail.get(key) for key in required):
        raise ValueError('角色面板、技能、共鸣链或突破材料不完整，未导入')
    report = build.BuildReport(version=version, locale=locale)
    with tempfile.TemporaryDirectory(prefix='character-supplement-') as scratch:
        staged = db.connect(str(Path(scratch) / 'knowledge.db'))
        try:
            with closing(sqlite3.connect(target_path.as_uri() + '?mode=ro', uri=True)) as original:
                original.backup(staged)
            existing = staged.execute('SELECT name,game_version FROM characters WHERE id=?', (cid,)).fetchone()
            if existing:
                raw = staged.execute('SELECT sha256 FROM raw_docs WHERE kind=\'character\' AND doc_id=? AND game_version=? AND locale=?',
                                     (cid, version, locale)).fetchone()
                if tuple(existing) == (expected_name, version) and raw and raw[0] == used[detail_path]['sha256']:
                    return {'status': 'unchanged', 'character_id': cid, 'name': expected_name, 'game_version': version}
                raise ValueError('该角色已存在且来源不同；须显式重建，禁止以补充操作覆盖')
            norm = normalize.Normalized()
            normalize.normalize_characters({cid: roster[cid]}, {cid: detail}, version, norm)
            # New related entries retain their own patch; existing items/equipment stay intact.
            adapters = (
                ('weapon.json', 'weapon', 'weapons', normalize.normalize_weapons),
                ('echo.json', 'echo', 'echoes', normalize.normalize_echoes),
                ('sonata.json', 'sonata', 'sonatas', lambda d,v,n: normalize.normalize_sonatas(d,v,locale,n)),
                ('monster.json', 'monster', 'monsters', normalize.normalize_monsters),
                (f'{locale}/item.json', 'item', 'items', normalize.normalize_items),
                (f'{locale}/hyperlink.json', 'hyperlink', 'hyperlinks', lambda d,v,n: normalize.normalize_hyperlinks(d,n)),
            )
            for rel, kind, table, adapter in adapters:
                payload = read(rel, kind)
                present = {str(r[0]) for r in staged.execute(f'SELECT id FROM {table}')}
                additions = {key: value for key,value in payload.items() if str(key) not in present}
                adapter(additions, version, norm)
                if kind in ('weapon', 'echo'):
                    for ident in additions:
                        rel_detail = f'{locale}/{kind}/{ident}.json'
                        if rel_detail not in manifest.get('files', {}):
                            continue
                        value = read(rel_detail, kind + '_detail')
                        if str(value.get('id')) != str(ident):
                            raise ValueError('装备ID与详情不一致')
                        (equipment.normalize_weapon if kind == 'weapon' else equipment.normalize_echo)(value, norm)
            build._store_raw(version, locale, used, norm)
            report.capability = db.init_schema(staged, reset=False)
            build._insert_all(staged, norm.tables, report)
            origin = used[detail_path]['url']
            for r in norm.tables.get('character_recommend', []):
                if r['kind'] == 'weapon':
                    staged.execute('INSERT INTO entity_relations VALUES (?,?,?,?,?,?,?,?,?)',
                        ('character', cid, 'recommended_weapon', 'weapon', str(r['ref_id']),
                         f"recommend.weapon[{r['idx']}]", origin, 'source_field', version))
            for ident in {r['item_key'] for r in norm.tables.get('character_ascensions', [])}:
                staged.execute('INSERT INTO entity_relations VALUES (?,?,?,?,?,?,?,?,?)',
                    ('character', cid, 'ascension_material', 'item', str(ident), 'ascensions', origin, 'source_field', version))
            build._build_fts(staged, report)
            db.set_meta(staged, 'character_supplement/' + cid,
                        json.dumps({'name':expected_name,'game_version':version,'sha256':used[detail_path]['sha256']}))
            staged.commit()
            if staged.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('补充库完整性检查失败，未发布')
            with closing(sqlite3.connect(target_path)) as target:
                staged.backup(target)
        finally:
            staged.close()
    return {'status':'added','character_id':cid,'name':expected_name,'game_version':version,
            'counts':report.counts,'notes':report.notes}
