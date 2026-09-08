'''
paths.py — repo-relative file locations.

several modules write files back into the repo (the season ledger, the recap
markdown) or read a data file out of it (the retrosheet parquet), and each was
re-deriving the repo root from its own __file__. one definition here keeps them
in agreement.
'''

import os

# highlights/ lives directly under the repo root
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def repo_path(*parts):
    '''absolute path to a file or directory inside the repo.'''
    return os.path.join(REPO_ROOT, *parts)
