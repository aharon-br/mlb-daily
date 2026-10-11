'''regression tests for bugs found auditing the rarity engine against real data.

run from the repo root with: python -m pytest tests
'''

import datetime
import json
import os

import pandas as pd
import pytest

from highlights import corpus, rarity, recaps

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')


@pytest.fixture(autouse=True)
def _no_ledger_writes(monkeypatch):
    monkeypatch.setattr(rarity, '_write_season_ledger', lambda *a, **k: None)
    monkeypatch.setattr(rarity, '_load_season_ledger', lambda *a, **k: {})
    monkeypatch.setattr(rarity, '_load_pattern_corpus', lambda: pd.DataFrame(
        columns=['date', 'team', 'pattern', 'description', 'game_id']))


def _real_plays_box():
    '''the first two innings of KC @ BOS on 2026-09-13, trimmed from the live feed.
    its playsByInning entries have no "num" key, just like the real API.'''
    with open(os.path.join(FIXTURES, 'kc_bos_2026-09-13_plays.json')) as fh:
        return json.load(fh)


# ---- inning numbers from the real live feed shape

def test_real_feed_entries_have_no_num_key():
    box = _real_plays_box()
    assert all('num' not in entry for entry in box['play_by_play']['playsByInning'])


def test_inning_number_reads_the_plays():
    plays = _real_plays_box()['play_by_play']
    assert rarity._inning_number(plays['playsByInning'][1], plays['allPlays'], 2) == 2


def test_inning_number_falls_back_to_position():
    assert rarity._inning_number({'top': [], 'bottom': []}, [], 7) == 7


def test_immaculate_inning_in_real_feed_shape():
    # used to raise ValueError from _ordinal('?') and fail the whole daily run
    events = rarity._check_immaculate_inning(_real_plays_box())
    assert len(events) == 1
    assert events[0]['kind'] == 'immaculate_inning'
    assert 'Payton Tolle' in events[0]['description']
    assert 'in the 1st' in events[0]['description']


def test_find_rare_events_survives_immaculate_inning_day():
    box = _real_plays_box()
    events = rarity.find_rare_events([box], datetime.date(2026, 9, 13))
    assert [e['kind'] for e in events] == ['immaculate_inning']


def test_inning_hr_patterns_use_real_inning():
    box = _real_plays_box()
    plays = box['play_by_play']['allPlays']
    # turn the second inning's bottom half into back-to-back homers
    for index in (12, 13):
        plays[index]['result']['event'] = 'Home Run'
    patterns = rarity._extract_inning_hr_patterns(box)
    assert [p['pattern'] for p in patterns] == ['back_to_back_hr_inning']
    assert 'in the 2nd inning' in patterns[0]['description']


# ---- one failing check must not cost the day

def test_failing_tier1_check_is_isolated(monkeypatch):
    def boom(box):
        raise ValueError('bad box score')

    monkeypatch.setattr(rarity, '_check_immaculate_inning', boom)
    box = _real_plays_box()
    assert rarity.find_rare_events([box], datetime.date(2026, 9, 13)) == []


def test_failing_tier3_extractor_is_isolated():
    def boom(box):
        raise ValueError('bad play feed')

    assert rarity._safely(boom, {}) == []


# ---- tier 1: perfect games

def _no_hit_box(batters_faced, walks=0, hbp=0):
    return {
        'teamInfo': {'away': {'teamName': 'Away'}, 'home': {'teamName': 'Home'}},
        'away': {'pitchers': [1], 'players': {'ID1': {
            'person': {'fullName': 'Ace Pitcher'},
            'stats': {'pitching': {
                'inningsPitched': '9.0', 'hits': 0, 'baseOnBalls': walks,
                'hitBatsmen': hbp, 'strikeOuts': 10, 'battersFaced': batters_faced,
            }},
        }}},
        'home': {'pitchers': [], 'players': {}},
    }


def test_perfect_game():
    events = rarity._check_no_hitter(_no_hit_box(27))
    assert [e['kind'] for e in events] == ['perfect_game']


def test_no_hitter_with_an_error_is_not_perfect():
    events = rarity._check_no_hitter(_no_hit_box(28))
    assert [e['kind'] for e in events] == ['no_hitter']


def test_no_hitter_with_a_walk_is_not_perfect():
    events = rarity._check_no_hitter(_no_hit_box(28, walks=1))
    assert [e['kind'] for e in events] == ['no_hitter']


# ---- tier 2: the season ledger

def _hit_box(pid, hits):
    return {
        'teamInfo': {'away': {'teamName': 'Away'}, 'home': {'teamName': 'Home'}},
        'away': {'batters': [pid], 'players': {f'ID{pid}': {
            'person': {'fullName': 'Streaky Hitter'},
            'stats': {'batting': {'hits': hits, 'atBats': 4}},
        }}},
        'home': {'batters': [], 'players': {}},
    }


def test_rerunning_a_day_does_not_extend_a_streak(monkeypatch):
    ledger = {'hitting_streaks': {'9': {'name': 'Streaky Hitter', 'team': 'Away',
                                        'streak': 19, 'last_date': '2026-09-12'}}}
    monkeypatch.setattr(rarity, '_load_season_ledger', lambda *a, **k: ledger)
    day = datetime.date(2026, 9, 13)

    events, new_ledger = rarity._check_season_ledger([_hit_box(9, 1)], day)
    assert new_ledger['hitting_streaks']['9']['streak'] == 20
    assert [e['kind'] for e in events] == ['hit_streak']

    # the workflow ran again for the same day
    monkeypatch.setattr(rarity, '_load_season_ledger', lambda *a, **k: new_ledger)
    events, again = rarity._check_season_ledger([_hit_box(9, 1)], day)
    assert again['hitting_streaks']['9']['streak'] == 20
    assert events == []


def test_ledger_year_follows_the_recap_date(monkeypatch):
    seen = []
    monkeypatch.setattr(rarity, '_load_season_ledger', lambda year=None: seen.append(year) or {})
    rarity._check_season_ledger([], datetime.date(2025, 9, 28))
    assert seen == [2025]


# ---- tier 3: franchise codes and de-duplication

def test_marlins_look_up_both_franchise_codes():
    assert rarity._franchise_codes('MIA') == ['MIA', 'FLO']
    assert rarity._franchise_codes('LAA') == ['LAA', 'ANA', 'CAL']
    assert rarity._franchise_codes('BOS') == ['BOS']


def test_marlins_last_seen_uses_the_miami_years(monkeypatch):
    monkeypatch.setattr(rarity, '_load_pattern_corpus', lambda: pd.DataFrame([
        {'team': 'FLO', 'pattern': 'position_player_pitching', 'date': datetime.date(2011, 7, 20)},
        {'team': 'MIA', 'pattern': 'position_player_pitching', 'date': datetime.date(2024, 9, 19)},
    ]))
    box = {
        'teamInfo': {'away': {'abbreviation': 'MIA', 'teamName': 'Marlins'},
                     'home': {'abbreviation': 'ZZZ', 'teamName': 'ZZZ'}},
        'away': {'batters': [], 'pitchers': [100], 'teamStats': {}, 'players': {'ID100': {
            'person': {'fullName': 'Position Player'}, 'position': {'abbreviation': 'RF'},
            'allPositions': [{'abbreviation': 'RF'}],
            'stats': {'pitching': {'inningsPitched': '1.0'}}}}},
        'home': {'batters': [], 'pitchers': [], 'players': {}, 'teamStats': {}},
    }
    events = rarity.find_rare_events([box], datetime.date(2028, 5, 1))
    position_events = [e for e in events if e['kind'] == 'position_player_pitching']
    assert len(position_events) == 1  # tier 3 annotates the tier 1 event, no duplicate
    assert position_events[0]['since'] == '2024-09-19'


# ---- walk-off grand slam in the live feed

def _walk_off_box(home_score, away_score, last_play=True, runners=('1B', '2B', '3B')):
    plays = [
        {'result': {'event': 'Strikeout'}, 'runners': []},
        {'result': {'event': 'Home Run', 'homeScore': home_score, 'awayScore': away_score},
         'matchup': {'batter': {'fullName': 'Slammer'}},
         'runners': [{'movement': {'start': base, 'end': 'score'}} for base in runners]
                    + [{'movement': {'start': None, 'end': 'score'}}]},
    ]
    if not last_play:
        plays.append({'result': {'event': 'Strikeout'}, 'runners': []})
    return {
        'teamInfo': {'away': {'abbreviation': 'BOS', 'teamName': 'Red Sox'},
                     'home': {'abbreviation': 'NYY', 'teamName': 'Yankees'}},
        'play_by_play': {'allPlays': plays, 'playsByInning': [{'top': [], 'bottom': list(range(len(plays)))}]},
    }


def test_walk_off_grand_slam_detected():
    assert len(rarity._extract_walk_off_grand_slam(_walk_off_box(6, 5))) == 1


def test_grand_slam_that_only_ties_is_not_a_walk_off():
    # down 4, the slam ties it at 4 and the game goes on
    assert rarity._extract_walk_off_grand_slam(_walk_off_box(4, 4)) == []


def test_grand_slam_with_more_game_left_is_not_a_walk_off():
    assert rarity._extract_walk_off_grand_slam(_walk_off_box(6, 5, last_play=False)) == []


# ---- corpus detectors (retrosheet cwevent rows)

def _event(game='NYA202405010', inn=9, home=1, event=2, **extra):
    row = {'GAME_ID': game, 'INN_CT': inn, 'BAT_HOME_ID': home, 'EVENT_CD': event,
           'BAT_ID': 'bat001', 'PIT_ID': 'pit001', 'HOME_TEAM_ID': 'NYA', 'AWAY_TEAM_ID': 'BOS',
           'HOME_SCORE_CT': 0, 'AWAY_SCORE_CT': 0, 'BASE1_RUN_ID': '', 'BASE2_RUN_ID': '',
           'BASE3_RUN_ID': '', 'PITCH_SEQ_TX': '', 'batting_team': 'NYA' if home else 'BOS',
           'date': '2024-05-01'}
    row.update(extra)
    return row


def _slam(**extra):
    return _event(event=corpus.EVENT_HR, BASE1_RUN_ID='a', BASE2_RUN_ID='b', BASE3_RUN_ID='c', **extra)


def test_corpus_walk_off_slam_uses_the_score_before_the_swing():
    game = pd.DataFrame([_event(inn=9, home=0), _slam(HOME_SCORE_CT=2, AWAY_SCORE_CT=5)])
    assert len(corpus._detect_walk_off_grand_slam(game, 'NYA202405010')) == 1


def test_corpus_slam_by_a_team_already_ahead_is_not_a_walk_off():
    # home team leads 7-2 in the 8th, hits a slam, and the game goes to the 9th
    game = pd.DataFrame([
        _slam(inn=8, HOME_SCORE_CT=7, AWAY_SCORE_CT=2),
        _event(inn=9, home=0),
    ])
    assert corpus._detect_walk_off_grand_slam(game, 'NYA202405010') == []


def test_corpus_slam_that_only_ties_is_not_a_walk_off():
    game = pd.DataFrame([_slam(HOME_SCORE_CT=0, AWAY_SCORE_CT=4)])
    assert corpus._detect_walk_off_grand_slam(game, 'NYA202405010') == []


def _strikeouts(*sequences, pitchers=('pit001',) * 3):
    return pd.DataFrame([
        _event(inn=3, home=0, event=corpus.EVENT_STRIKEOUT, PITCH_SEQ_TX=seq, PIT_ID=pitchers[i])
        for i, seq in enumerate(sequences)
    ])


def test_corpus_immaculate_inning_basic():
    assert len(corpus._detect_immaculate_inning(_strikeouts('CSS', 'SSS', 'CCS'), 'g')) == 1


def test_corpus_immaculate_inning_counts_fouls_as_strikes():
    # FFS is a 3-pitch strikeout: two fouls (strikes) then a swinging strike
    assert len(corpus._detect_immaculate_inning(_strikeouts('FFS', 'SSS', 'CSS'), 'g')) == 1


def test_corpus_a_foul_cannot_be_the_third_strike():
    assert corpus._detect_immaculate_inning(_strikeouts('SSF', 'SSS', 'CSS'), 'g') == []


def test_corpus_a_ball_rules_out_an_immaculate_inning():
    assert corpus._detect_immaculate_inning(_strikeouts('BSS', 'SSS', 'CSS'), 'g') == []


def test_corpus_needs_one_pitcher_for_all_three():
    game = _strikeouts('CSS', 'SSS', 'CCS', pitchers=('pit001', 'pit001', 'pit002'))
    assert corpus._detect_immaculate_inning(game, 'g') == []


# ---- recaps are written in date order and reruns replace the day

def _write(tmp_path, day):
    recaps.write_recap(datetime.date(2026, 9, day), games=[], top_lines=[],
                       rare_events=[], summary=f'summary {day}', recaps_dir=str(tmp_path))


def _headings(tmp_path):
    text = (tmp_path / '2026.md').read_text()
    return [line for line in text.splitlines() if line.startswith('## ')]


def test_recap_backfill_lands_in_date_order(tmp_path):
    for day in (15, 13, 14):
        _write(tmp_path, day)
    assert _headings(tmp_path) == ['## September 15, 2026', '## September 14, 2026', '## September 13, 2026']


def test_recap_rerun_replaces_instead_of_duplicating(tmp_path):
    _write(tmp_path, 13)
    _write(tmp_path, 14)
    _write(tmp_path, 13)
    assert _headings(tmp_path) == ['## September 14, 2026', '## September 13, 2026']
