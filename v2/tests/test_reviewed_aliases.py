"""Reviewed names survive rebuilds without changing canonical character identity."""
import unittest
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from knowledge.normalize import Normalized,normalize_characters
from knowledge.reviewed_aliases import character_aliases


class ReviewedAliasTests(unittest.TestCase):
    def test_alias_is_bound_to_id_and_expected_canonical_name(self):
        self.assertEqual(character_aliases('1311','心')[0]['alias'],'心月狐')
        self.assertEqual(character_aliases('1211','心'),[])
        self.assertEqual(character_aliases('1311','其他角色'),[])

    def test_rebuild_keeps_canonical_name_and_review_provenance(self):
        norm=Normalized()
        with patch('knowledge.normalize.load_map',return_value={}):
            normalize_characters({'1311':{'zh':'心'}},{},'3.7',norm)
        self.assertEqual(norm.tables['characters'][0]['name'],'心')
        alias=next(a for a in norm.tables['aliases'] if a['alias']=='心月狐')
        self.assertEqual(alias['entity_id'],'1311')
        self.assertEqual(alias['source'],'reviewed_source:mcguide/1311/10232@3')


if __name__=='__main__':unittest.main()
