"""Offline addition of one new character; requires hash-verified versioned source mirrors."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from knowledge.config import DB_PATH
from knowledge.supplements import add_character


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--version', required=True)
    ap.add_argument('--character-id', required=True)
    ap.add_argument('--expected-name', required=True)
    ap.add_argument('--db', default=DB_PATH)
    args = ap.parse_args()
    result = add_character(version=args.version, character_id=args.character_id,
                           expected_name=args.expected_name, db_path=args.db)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
