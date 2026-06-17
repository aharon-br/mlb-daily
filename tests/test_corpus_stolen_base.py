'''tests for the stolen_base_burst corpus detector.

stolen bases are baserunning events (BAT_EVENT_FL == 'F'), so the main thing
these lock in is that the detector sees steals the other detectors' batting
filter throws away. no Chadwick/cwevent needed — we feed synthetic frames
shaped like cwevent output.

run from the repo root with: python -m pytest tests/test_corpus_stolen_base.py
'''

import pandas as pd
import pytest

from highlights import corpus


def _steal_row(game_id='NYA20230615', bat_home=1, home='NYA', away='BOS'):
    '''one stolen-base event row (BAT_EVENT_FL == 'F', EVENT_CD == 4).'''
    return {
        'GAME_ID': game_id, 'BAT_EVENT_FL': 'F', 'INN_CT': 5,
        'BAT_HOME_ID': bat_home, 'EVENT_CD': corpus.EVENT_STOLEN_BASE,
        'BAT_ID': 'bat001', 'HOME_TEAM_ID': home, 'AWAY_TEAM_ID': away,
        'HOME_SCORE_CT': 0, 'AWAY_SCORE_CT': 0,
    }


def _batting_row(game_id='NYA20230615', bat_home=1, home='NYA', away='BOS'):
    '''a benign true batting event (a strikeout) so `batting` isn't empty.'''
    row = _steal_row(game_id, bat_home, home, away)
    row.update({'BAT_EVENT_FL': 'T', 'EVENT_CD': corpus.EVENT_STRIKEOUT})
    return row


# ---- unit: the detector itself ----------------------------------------------

def _game_df(rows):
    df = pd.DataFrame(rows)
    df['batting_team'] = df.apply(corpus._batting_team, axis=1)
    df['date'] = df['GAME_ID'].apply(corpus._game_id_to_date)
    return df


def test_detector_fires_at_threshold():
    df = _game_df([_steal_row() for _ in range(4)])
    out = corpus._detect_stolen_base_burst(df, 'NYA20230615')
    assert len(out) == 1
    assert out[0]['team'] == 'NYA'
    assert out[0]['pattern'] == 'stolen_base_burst'
    assert out[0]['date'] == '2023-06-15'
    assert '4 stolen bases' in out[0]['description']


def test_detector_below_threshold_is_silent():
    df = _game_df([_steal_row() for _ in range(3)])
    assert corpus._detect_stolen_base_burst(df, 'NYA20230615') == []


def test_detector_counts_per_team_not_per_game():
    # 3 steals each for home (NYA) and away (BOS) — neither team hits 4.
    rows = [_steal_row(bat_home=1) for _ in range(3)] + \
           [_steal_row(bat_home=0) for _ in range(3)]
    assert corpus._detect_stolen_base_burst(_game_df(rows), 'NYA20230615') == []

    # give the away team a 4th — only BOS should surface.
    rows.append(_steal_row(bat_home=0))
    out = corpus._detect_stolen_base_burst(_game_df(rows), 'NYA20230615')
    assert [r['team'] for r in out] == ['BOS']


def test_double_steal_counts_as_one_event():
    '''documents the known caveat: retrosheet logs a multi-runner steal as a
    single EVENT_CD==4 row, so three single steals + one double steal is 4
    events (fires), not 5 bases.'''
    df = _game_df([_steal_row() for _ in range(4)])  # 4 event rows
    out = corpus._detect_stolen_base_burst(df, 'NYA20230615')
    assert '4 stolen bases' in out[0]['description']


# ---- integration: survives the BAT_EVENT_FL filter --------------------------

def test_steals_survive_the_batting_filter():
    '''the regression that matters: _extract_patterns_from_events filters to
    BAT_EVENT_FL == 'T' for the other detectors, but the steal rows are 'F'.
    they must still be scanned and produce a stolen_base_burst row.'''
    rows = [_batting_row()] + [_steal_row() for _ in range(4)]
    events = pd.DataFrame(rows)

    out = corpus._extract_patterns_from_events(events, 2023)

    bursts = [r for r in out if r['pattern'] == 'stolen_base_burst']
    assert len(bursts) == 1
    assert bursts[0]['team'] == 'NYA'
    assert bursts[0]['date'] == '2023-06-15'
