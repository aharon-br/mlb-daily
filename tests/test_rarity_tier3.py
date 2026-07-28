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


def test_team_map_resolves_to_the_recent_franchise_era():
    '''existence in the corpus isn't enough — the mapped code must reach the
    club's *recent* history, otherwise "first time since" always points decades
    back. e.g. the Marlins live as "MIA" (2012+) and "FLO" (1993-2011); mapping
    "MIA" -> "FLO" would strand every game in the pre-2012 era.'''
    corpus = rarity._load_pattern_corpus()
    latest = corpus.groupby('team').date.max()
    # every current club must have corpus history within the last ~5 seasons of
    # the corpus's own coverage (the corpus ends in 2024).
    corpus_end = corpus.date.max()
    cutoff = corpus_end - datetime.timedelta(days=365 * 5)
    stale = {
        abbrev: latest.get(rarity._to_retrosheet_code(abbrev))
        for abbrev in CURRENT_STATSAPI_ABBREVS
        if latest.get(rarity._to_retrosheet_code(abbrev), datetime.date.min) < cutoff
    }
    assert not stale, f'these clubs map to a stale franchise era: {stale}'


def test_real_corpus_dates_compare_against_target_date():
    '''the shipped parquet stores dates as strings; _load_pattern_corpus must
    coerce them to datetime.date so `corpus.date < target_date` and the gap
    subtraction work. this guards the conversion that the monkeypatched-corpus
    tests bypass — a regression there would silently kill tier 3 inside the
    broad tier-3 except.'''
    corpus = rarity._load_pattern_corpus()
    assert isinstance(corpus.date.iloc[0], datetime.date)

    target = datetime.date(2025, 6, 1)
    # a plain boolean-mask comparison must not raise (str vs date would).
    prior = corpus[(corpus.team == 'BOS') & (corpus.date < target)]
    assert not prior.empty
    gap = (target - prior.date.max()).days
    assert gap >= 0


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
