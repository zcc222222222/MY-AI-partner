"""New-patch character supplements preserve existing data and reject corrupt mirrors."""
import hashlib
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from knowledge import config, db, nanoka
from knowledge.scopes import entity_version
from knowledge.supplements import add_character


class CharacterSupplementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'knowledge.db'
        conn = db.connect(str(self.path))
        db.init_schema(conn)
        conn.execute("INSERT INTO characters(id,name,game_version) VALUES(1,'Old','3.7')")
        conn.execute("INSERT INTO weapons(id,name,game_version) VALUES(7,'Old weapon','3.7')")
        db.set_meta(conn, 'game_version', '3.7')
        db.set_meta(conn, 'locale', 'zh')
        conn.commit();conn.close()
        self.patch = patch.object(config, 'RAW_DIR', str(self.root / 'raw'))
        self.patch.start();self.addCleanup(self.patch.stop)
        detail = {'id':1512, 'name':'New', 'rarity':5,
                  'stats':{'0':{'1':{'life':100,'atk':20,'def':10}}},
                  'skill_trees':{'1':{'skill':{'name':'Skill','desc':'Heals 100.'}}},
                  'chains':{'1':{'name':'Chain','desc':'Improves healing.'}},
                  'ascensions':{'0':[{'key':99,'value':2}]},
                  'recommend':{'weapon':[7,8]}}
        files = {
            'character.json':('roster',{'1512':{'zh':'New','en':'New alias','element':5,'weapon':3},'1':{'zh':'Changed'}}),
            'zh/character/1512.json':('character',detail),
            'weapon.json':('weapon',{'7':{'zh':'Changed weapon'},'8':{'zh':'New weapon','rank':5}}),
            'echo.json':('echo',{}), 'sonata.json':('sonata',{}), 'monster.json':('monster',{}),
            'zh/item.json':('item',{'99':{'name':'New material','rank':3}}),
            'zh/hyperlink.json':('hyperlink',{}),
        }
        manifest = nanoka.load_manifest('3.8')
        for rel,(kind,value) in files.items():
            raw = json.dumps(value).encode()
            dest = Path(nanoka.local_path('3.8',rel));dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(raw)
            manifest['files'][rel] = {'kind':kind,'doc_id':'1512' if kind=='character' else Path(rel).name,
                'sha256':hashlib.sha256(raw).hexdigest(),'url':'https://static.nanoka.cc/ww/3.8/'+rel}
        nanoka.save_manifest('3.8',manifest)

    def add(self):
        return add_character(version='3.8',character_id='1512',expected_name='New',db_path=str(self.path))

    def test_complete_addition_preserves_old_rows_versions_and_material_names(self):
        self.assertEqual(self.add()['status'],'added')
        with closing(db.connect(str(self.path))) as c:
            self.assertEqual(tuple(c.execute('SELECT name,game_version FROM characters WHERE id=1').fetchone()),('Old','3.7'))
            self.assertEqual(tuple(c.execute('SELECT name,game_version FROM weapons WHERE id=7').fetchone()),('Old weapon','3.7'))
            self.assertEqual(entity_version(c,'character','New alias'),'3.8')
            self.assertEqual(entity_version(c,'character','Old'),'3.7')
            self.assertEqual(entity_version(c),'3.7')
            self.assertEqual(c.execute('SELECT count(*) FROM character_skills WHERE character_id=1512').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT name FROM items WHERE id=99').fetchone()[0],'New material')
            self.assertEqual(c.execute('SELECT game_version FROM entity_relations WHERE from_id=\'1512\' LIMIT 1').fetchone()[0],'3.8')
            self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        self.assertEqual(self.add()['status'],'unchanged')

    def test_corrupt_or_misidentified_source_never_changes_live_database(self):
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            add_character(version='3.8',character_id='1512',expected_name='Wrong',db_path=str(self.path))
        self.assertEqual(self.path.read_bytes(),before)
        Path(nanoka.local_path('3.8','zh/character/1512.json')).write_text('{}')
        with self.assertRaisesRegex(ValueError,'校验失败'):self.add()
        self.assertEqual(self.path.read_bytes(),before)

    def test_missing_dependency_source_never_partially_publishes_character(self):
        before = self.path.read_bytes()
        Path(nanoka.local_path('3.8','zh/item.json')).unlink()
        with self.assertRaises(ValueError):self.add()
        self.assertEqual(self.path.read_bytes(),before)

    def test_existing_identity_cannot_be_overwritten_from_another_patch(self):
        with closing(db.connect(str(self.path))) as c, c:
            c.execute("INSERT INTO characters(id,name,game_version) VALUES(1512,'Historical','3.7')")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError,'禁止.*覆盖'):self.add()
        self.assertEqual(self.path.read_bytes(),before)


if __name__ == '__main__': unittest.main()
