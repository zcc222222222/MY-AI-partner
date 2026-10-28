"""Run offline tests with operational diagnostics isolated from the live dashboard."""
import argparse
import os
from pathlib import Path
import tempfile
import unittest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pattern',default='test*.py')
    args=parser.parse_args()
    previous=os.environ.get('RETRIEVAL_DIAGNOSTICS_DIR')
    try:
        with tempfile.TemporaryDirectory(prefix='aipartner-test-diagnostics-') as directory:
            os.environ['RETRIEVAL_DIAGNOSTICS_DIR']=directory
            suite=unittest.defaultTestLoader.discover(str(Path(__file__).resolve().parents[1]/'tests'),pattern=args.pattern)
            result=unittest.TextTestRunner().run(suite)
            return 0 if result.wasSuccessful() else 1
    finally:
        if previous is None:os.environ.pop('RETRIEVAL_DIAGNOSTICS_DIR',None)
        else:os.environ['RETRIEVAL_DIAGNOSTICS_DIR']=previous


if __name__=='__main__':raise SystemExit(main())
