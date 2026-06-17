'''regression tests for the tier 3 (historical corpus) rarity matching.

these lock in the two fixes that made tier 3 actually surface events:
  1. StatsAPI team abbreviations are translated to retrosheet franchise codes
     before joining the corpus (previously ~1/3 of clubs never matched).
  2. the gap threshold is per-pattern, not a single flat 5-year cutoff.

run from the repo root with: python -m pytest tests/test_rarity_tier3.py
'''

import datetime

import pandas as pd
import pytest

from highlights import rarity


# the 30 current franchises' StatsAPI abbreviations. every one of these must
# translate to a retrosheet code that actually exists in the corpus, otherwise
# tier 3 silently goes dark for that club.
CURRENT_STATSAPI_ABBREVS = [
    'ARI', 'ATL', 'BAL', 'BOS', 'CHC', 'CWS', 'CIN', 'CLE', 'COL', 'DET',
    'HOU', 'KC', 'LAA', 'LAD', 'MIA', 'MIL', 'MIN', 'NYM', 'NYY', 'OAK',
    'PHI', 'PIT', 'SD', 'SF', 'SEA', 'STL', 'TB', 'TEX', 'TOR', 'WSH',
]


def _box_with_position_player_pitching(abbrev):
    '''minimal box score where the away club's lone "pitcher" is a position
    player, so _extract_patterns emits exactly one position_player_pitching
    pattern for `abbrev` and nothing else (no play-by-play, no team steals).
    '''
    return {
        'teamInfo': {
            'away': {'abbreviation': abbrev, 'teamName': abbrev},
            'home': {'abbreviation': 'ZZZ', 'teamName': 'ZZZ'},
        },
        'away': {
            'batters': [],
            'pitchers': [100],
            'players': {
                'ID100': {
                    'person': {'fullName': 'Position Player'},
                    'position': {'abbreviation': 'RF'},
                    'allPositions': [{'abbreviation': 'RF'}],
                },
            },
            'teamStats': {},
        },
        'home': {'batters': [], 'pitchers': [], 'players': {}, 'teamStats': {}},
    }


@pytest.fixture(autouse=True)
def _no_ledger_writes(monkeypatch):
    # keep tier 2 from touching data/season-ledger-*.json during the test.
    monkeypatch.setattr(rarity, '_write_season_ledger', lambda *a, **k: None)
    monkeypatch.setattr(rarity, '_load_season_ledger', lambda *a, **k: {})


def _patch_corpus(monkeypatch, rows):
    df = pd.DataFrame(rows)
    monkeypatch.setattr(rarity, '_load_pattern_corpus', lambda: df)


def test_team_map_resolves_every_current_franchise_into_corpus():
    '''_to_retrosheet_code must land every current club on a code the real
    corpus contains — this catches the StatsAPI/retrosheet vocab drift.'''
    corpus_teams = set(rarity._load_pattern_corpus().team.unique())
    unresolved = {
        abbrev: rarity._to_retrosheet_code(abbrev)
        for abbrev in CURRENT_STATSAPI_ABBREVS
        if rarity._to_retrosheet_code(abbrev) not in corpus_teams
    }
    assert not unresolved, f'these clubs never join the corpus: {unresolved}'


def test_mismatched_franchise_now_surfaces_tier3_event(monkeypatch):
    '''a Dodgers (StatsAPI "LAD") game must find its retrosheet ("LAN") history.
    before the fix the join compared "LAD" to "LAN" and always missed.'''
    last = datetime.date(2014, 5, 1)
    target = datetime.date(2025, 6, 1)
    _patch_corpus(monkeypatch, [
        {'team': 'LAN', 'pattern': 'position_player_pitching', 'date': last},
    ])

    events = rarity.find_rare_events([_box_with_position_player_pitching('LAD')], target)

    tier3 = [e for e in events if e['kind'] == 'position_player_pitching' and e.get('since')]
    assert len(tier3) == 1
    assert tier3[0]['since'] == last.isoformat()


def test_threshold_is_per_pattern(monkeypatch):
    '''a gap inside the pattern's fuse stays hidden; a gap past it surfaces.
    position_player_pitching uses a 3-year fuse.'''
    target = datetime.date(2025, 6, 1)

    # ~1 year ago: under the 3-year fuse -> suppressed
    _patch_corpus(monkeypatch, [
        {'team': 'LAN', 'pattern': 'position_player_pitching',
         'date': datetime.date(2024, 6, 1)},
    ])
    recent = rarity.find_rare_events([_box_with_position_player_pitching('LAD')], target)
    assert not [e for e in recent if e['kind'] == 'position_player_pitching' and e.get('since')]

    # ~4 years ago: past the 3-year fuse -> surfaces
    _patch_corpus(monkeypatch, [
        {'team': 'LAN', 'pattern': 'position_player_pitching',
         'date': datetime.date(2021, 5, 1)},
    ])
    old = rarity.find_rare_events([_box_with_position_player_pitching('LAD')], target)
    assert [e for e in old if e['kind'] == 'position_player_pitching' and e.get('since')]


def test_identity_codes_still_match(monkeypatch):
    '''clubs whose StatsAPI abbrev already equals the retrosheet code (e.g. BOS)
    must keep working through the default passthrough.'''
    assert rarity._to_retrosheet_code('BOS') == 'BOS'
    last = datetime.date(2014, 5, 1)
    target = datetime.date(2025, 6, 1)
    _patch_corpus(monkeypatch, [
        {'team': 'BOS', 'pattern': 'position_player_pitching', 'date': last},
    ])
    events = rarity.find_rare_events([_box_with_position_player_pitching('BOS')], target)
    assert [e for e in events if e['kind'] == 'position_player_pitching' and e.get('since')]
