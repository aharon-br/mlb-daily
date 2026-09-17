'''unit tests for the offline retrosheet ETL.

the download/cwevent steps need network + the chadwick tools, so those are
monkeypatched. what's covered here is the pure logic: column normalization,
game-id date parsing, every pattern detector, and the parquet update path.
'''

import pandas as pd
import pytest

from highlights import corpus


def _event(game_id='BOS20240408', inn=1, bat_home=1, event_cd=corpus.EVENT_HR,
           bat_id='batter1', pit_id='pitcher1', pitch_seq='CSS',
           home_score=0, away_score=0, bases=('', '', ''), pit_start='F'):
    return {
        'GAME_ID': game_id,
        'BAT_EVENT_FL': 'T',
        'INN_CT': inn,
        'BAT_HOME_ID': bat_home,
        'EVENT_CD': event_cd,
        'BAT_ID': bat_id,
        'PIT_ID': pit_id,
        'PIT_START_FL': pit_start,
        'PITCH_SEQ_TX': pitch_seq,
        'HOME_TEAM_ID': 'BOS',
        'AWAY_TEAM_ID': 'NYA',
        'HOME_SCORE_CT': home_score,
        'AWAY_SCORE_CT': away_score,
        'BASE1_RUN_ID': bases[0],
        'BASE2_RUN_ID': bases[1],
        'BASE3_RUN_ID': bases[2],
    }


def _game_df(rows):
    df = pd.DataFrame(rows)
    df['batting_team'] = df.apply(corpus._batting_team, axis=1)
    df['date'] = df['GAME_ID'].apply(corpus._game_id_to_date)
    return df


def test_game_id_to_date():
    assert corpus._game_id_to_date('ANA200304080') == '2003-04-08'


def test_game_id_to_date_passes_through_bad_input():
    assert corpus._game_id_to_date(None) is None or corpus._game_id_to_date(12345) == 12345


def test_batting_team_picks_the_side_at_bat():
    row = {'BAT_HOME_ID': 1, 'HOME_TEAM_ID': 'BOS', 'AWAY_TEAM_ID': 'NYA'}
    assert corpus._batting_team(row) == 'BOS'
    row['BAT_HOME_ID'] = 0
    assert corpus._batting_team(row) == 'NYA'


def test_normalize_columns_renames_and_derives_home_team():
    df = pd.DataFrame([{
        'GAME_ID': 'BOS20240408', 'VISITING_TEAM': 'NYA', 'BATTING_TEAM': 1,
        'EVENT_TYPE': 23, 'PITCH_SEQUENCE': 'CSS', 'UNRELATED': 'x',
    }])
    out = corpus._normalize_columns(df)
    assert out['AWAY_TEAM_ID'].iloc[0] == 'NYA'
    assert out['BAT_HOME_ID'].iloc[0] == 1
    assert out['EVENT_CD'].iloc[0] == 23
    assert out['PITCH_SEQ_TX'].iloc[0] == 'CSS'
    assert out['HOME_TEAM_ID'].iloc[0] == 'BOS'
    assert 'UNRELATED' in out.columns


def test_detect_back_to_back_hr():
    df = _game_df([
        _event(bat_id='a'),
        _event(bat_id='b'),
    ])
    found = corpus._detect_back_to_back_hr_inning(df, 'BOS20240408')
    assert len(found) == 1
    assert found[0]['pattern'] == 'back_to_back_hr_inning'
    assert found[0]['team'] == 'BOS'
    assert found[0]['date'] == '2024-04-08'


def test_back_to_back_requires_consecutive_plate_appearances():
    df = _game_df([
        _event(bat_id='a'),
        _event(bat_id='walker', event_cd=14),   # walk in between
        _event(bat_id='b'),
    ])
    assert corpus._detect_back_to_back_hr_inning(df, 'BOS20240408') == []


def test_back_to_back_not_across_innings():
    df = _game_df([_event(inn=1, bat_id='a'), _event(inn=2, bat_id='b')])
    assert corpus._detect_back_to_back_hr_inning(df, 'BOS20240408') == []


def test_detect_three_hr_inning_allows_non_consecutive():
    df = _game_df([
        _event(bat_id='a'),
        _event(bat_id='out', event_cd=2),
        _event(bat_id='b'),
        _event(bat_id='c'),
    ])
    found = corpus._detect_three_hr_inning(df, 'BOS20240408')
    assert len(found) == 1
    assert '3 HRs in inning 1' in found[0]['description']


def test_two_hr_inning_is_not_a_three_hr_inning():
    df = _game_df([_event(bat_id='a'), _event(bat_id='b')])
    assert corpus._detect_three_hr_inning(df, 'BOS20240408') == []


def _walk_off_rows(home_score, away_score, bases=('r1', 'r2', 'r3')):
    return _game_df([
        _event(inn=9, bat_home=1, bat_id='hero', bases=bases,
               home_score=home_score, away_score=away_score),
    ])


def test_detect_walk_off_grand_slam():
    # home trailed 3-2 (2 before the swing), ends 6-3
    found = corpus._detect_walk_off_grand_slam(_walk_off_rows(6, 3), 'BOS20240408')
    assert len(found) == 1
    assert found[0]['pattern'] == 'walk_off_grand_slam'
    assert 'trailing 1' in found[0]['description']


def test_walk_off_needs_bases_loaded():
    assert corpus._detect_walk_off_grand_slam(
        _walk_off_rows(6, 3, bases=('r1', '', 'r3')), 'BOS20240408') == []


def test_walk_off_needs_the_home_team_to_win():
    assert corpus._detect_walk_off_grand_slam(_walk_off_rows(4, 9), 'BOS20240408') == []


def test_walk_off_ignores_slams_when_already_ahead():
    # home was up 5-0 before the slam — not a walk-off comeback
    assert corpus._detect_walk_off_grand_slam(_walk_off_rows(9, 0), 'BOS20240408') == []


def test_walk_off_returns_nothing_without_base_columns():
    df = _game_df([_event(inn=9, bat_home=1)]).drop(columns=['BASE2_RUN_ID'])
    assert corpus._detect_walk_off_grand_slam(df, 'BOS20240408') == []


def _immaculate_rows(seqs, event_cd=corpus.EVENT_STRIKEOUT):
    return _game_df([
        _event(bat_home=0, bat_id=f'b{i}', event_cd=event_cd, pitch_seq=seq)
        for i, seq in enumerate(seqs)
    ])


def test_detect_immaculate_inning():
    found = corpus._detect_immaculate_inning(_immaculate_rows(['CSS', 'SSS', 'CCK']),
                                             'BOS20240408')
    assert len(found) == 1
    assert found[0]['team'] == 'BOS'   # visitors batting -> home team pitching
    assert '9 pitches, 3 Ks' in found[0]['description']


def test_immaculate_inning_rejects_extra_pitches():
    assert corpus._detect_immaculate_inning(
        _immaculate_rows(['CSSS', 'SSS', 'CCK']), 'BOS20240408') == []


def test_immaculate_inning_rejects_balls_in_the_sequence():
    assert corpus._detect_immaculate_inning(
        _immaculate_rows(['CBS', 'SSS', 'CCK']), 'BOS20240408') == []


def test_immaculate_inning_rejects_a_fourth_batter():
    rows = _immaculate_rows(['CSS', 'SSS', 'CCK'])
    extra = _game_df([_event(bat_home=0, bat_id='b4', event_cd=corpus.EVENT_STRIKEOUT,
                             pitch_seq='CSS')])
    df = pd.concat([rows, extra], ignore_index=True)
    assert corpus._detect_immaculate_inning(df, 'BOS20240408') == []


def test_immaculate_inning_requires_three_strikeouts():
    assert corpus._detect_immaculate_inning(
        _immaculate_rows(['CSS', 'SSS', 'CCK'], event_cd=2), 'BOS20240408') == []


def test_immaculate_inning_needs_pitch_sequences():
    df = _immaculate_rows(['CSS', 'SSS', 'CCK']).drop(columns=['PITCH_SEQ_TX'])
    assert corpus._detect_immaculate_inning(df, 'BOS20240408') == []


def test_detect_position_player_pitching():
    # 'utility' bats in innings 1 and 2 for the visitors, then pitches in the 9th
    df = _game_df([
        _event(inn=1, bat_home=0, bat_id='utility', pit_id='starter', pit_start='T'),
        _event(inn=2, bat_home=0, bat_id='utility', pit_id='starter', pit_start='T'),
        _event(inn=9, bat_home=1, bat_id='slugger', pit_id='utility'),
    ])
    found = corpus._detect_position_player_pitching(df, 'BOS20240408')
    assert len(found) == 1
    assert found[0]['pattern'] == 'position_player_pitching'
    assert found[0]['team'] == 'NYA'   # home team batting -> visitors pitching


def test_relief_pitcher_who_also_batted_is_not_flagged():
    # classic NL false positive: reliever pitches first, then bats later
    df = _game_df([
        _event(inn=6, bat_home=1, bat_id='slugger', pit_id='reliever'),
        _event(inn=7, bat_home=0, bat_id='reliever', pit_id='starter', pit_start='T'),
    ])
    assert corpus._detect_position_player_pitching(df, 'BOS20240408') == []


def test_position_player_pitching_needs_pitcher_column():
    df = _game_df([_event()]).drop(columns=['PIT_ID'])
    assert corpus._detect_position_player_pitching(df, 'BOS20240408') == []


def test_extract_patterns_skips_years_missing_columns(capsys):
    df = pd.DataFrame([{'GAME_ID': 'BOS20240408'}])
    assert corpus._extract_patterns_from_events(df, 2024) == []
    assert 'missing columns' in capsys.readouterr().out


def test_extract_patterns_drops_non_batting_events():
    rows = [_event(bat_id='a'), _event(bat_id='b')]
    rows[1]['BAT_EVENT_FL'] = 'F'   # baserunning micro-event, must be ignored
    assert corpus._extract_patterns_from_events(pd.DataFrame(rows), 2024) == []


def test_extract_patterns_returns_detected_rows():
    rows = [_event(bat_id='a'), _event(bat_id='b')]
    found = corpus._extract_patterns_from_events(pd.DataFrame(rows), 2024)
    assert [row['pattern'] for row in found] == ['back_to_back_hr_inning']


@pytest.fixture
def _pattern_rows(monkeypatch):
    monkeypatch.setattr(corpus, '_load_events_for_year',
                        lambda year: pd.DataFrame([_event(bat_id='a'), _event(bat_id='b')]))


def test_update_corpus_creates_parquet(monkeypatch, tmp_path, _pattern_rows):
    out = tmp_path / 'patterns.parquet'
    corpus.update_corpus_for_year(2024, out_path=str(out))
    written = pd.read_parquet(out)
    assert list(written['pattern']) == ['back_to_back_hr_inning']


def test_update_corpus_replaces_existing_rows_for_that_year(monkeypatch, tmp_path,
                                                            _pattern_rows):
    out = tmp_path / 'patterns.parquet'
    pd.DataFrame([
        {'date': '2019-06-01', 'team': 'BOS', 'pattern': 'old_but_kept',
         'description': '', 'game_id': 'BOS20190601'},
        {'date': '2024-04-08', 'team': 'BOS', 'pattern': 'stale_and_replaced',
         'description': '', 'game_id': 'BOS20240408'},
    ]).to_parquet(out, index=False)

    corpus.update_corpus_for_year(2024, out_path=str(out))

    written = pd.read_parquet(out)
    assert set(written['pattern']) == {'old_but_kept', 'back_to_back_hr_inning'}


def test_update_corpus_no_op_when_no_events(monkeypatch, tmp_path):
    monkeypatch.setattr(corpus, '_load_events_for_year', lambda year: None)
    out = tmp_path / 'patterns.parquet'
    corpus.update_corpus_for_year(2024, out_path=str(out))
    assert not out.exists()


def test_update_corpus_no_op_when_loading_raises(monkeypatch, tmp_path):
    def boom(year):
        raise RuntimeError('cwevent not installed')

    monkeypatch.setattr(corpus, '_load_events_for_year', boom)
    out = tmp_path / 'patterns.parquet'
    corpus.update_corpus_for_year(2024, out_path=str(out))
    assert not out.exists()


def test_build_pattern_corpus_writes_all_years(monkeypatch, tmp_path):
    monkeypatch.setattr(corpus, '_load_events_for_year',
                        lambda year: pd.DataFrame([
                            _event(game_id=f'BOS{year}0408', bat_id='a'),
                            _event(game_id=f'BOS{year}0408', bat_id='b'),
                        ]))
    out = tmp_path / 'patterns.parquet'
    corpus.build_pattern_corpus(start_year=2023, end_year=2024, out_path=str(out))
    written = pd.read_parquet(out)
    assert sorted(written['date']) == ['2023-04-08', '2024-04-08']


def test_build_pattern_corpus_skips_bad_years(monkeypatch, tmp_path, capsys):
    def loader(year):
        if year == 2023:
            raise RuntimeError('download failed')
        return None

    monkeypatch.setattr(corpus, '_load_events_for_year', loader)
    out = tmp_path / 'patterns.parquet'
    corpus.build_pattern_corpus(start_year=2023, end_year=2024, out_path=str(out))
    assert not out.exists()
    assert 'no pattern occurrences found' in capsys.readouterr().out
