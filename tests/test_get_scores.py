'''unit tests for the MLB Stats API wrapper.

every statsapi call is monkeypatched — these tests never touch the network.
'''

import datetime

from highlights import get_scores


def _game(game_id, status='Final', game_type='R'):
    return {
        'game_id': game_id,
        'status': status,
        'game_type': game_type,
        'away_name': 'Yankees',
        'home_name': 'Red Sox',
    }


def test_yesterday_is_one_day_back():
    assert get_scores.yesterday() == datetime.date.today() - datetime.timedelta(days=1)


def test_games_for_date_keeps_only_final_real_games(monkeypatch):
    schedule = [
        _game(1, status='Final'),
        _game(2, status='Game Over'),
        _game(3, status='Completed Early'),
        _game(4, status='In Progress'),
        _game(5, status='Postponed'),
        _game(6, status='Final', game_type='E'),   # spring training
        _game(7, status='Final', game_type='S'),   # exhibition
        _game(8, status='Final', game_type='W'),   # world series
    ]
    monkeypatch.setattr(get_scores.statsapi, 'schedule', lambda **kwargs: schedule)

    kept = get_scores.games_for_date(datetime.date(2026, 4, 29))
    assert [g['game_id'] for g in kept] == [1, 2, 3, 8]


def test_games_for_date_passes_single_day_range(monkeypatch):
    captured = {}

    def fake_schedule(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(get_scores.statsapi, 'schedule', fake_schedule)
    get_scores.games_for_date(datetime.date(2026, 4, 29))
    assert captured == {'start_date': '2026-04-29', 'end_date': '2026-04-29'}


def test_games_for_date_returns_empty_on_api_error(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError('statsapi is down')

    monkeypatch.setattr(get_scores.statsapi, 'schedule', boom)
    assert get_scores.games_for_date(datetime.date(2026, 4, 29)) == []


def test_box_score_returns_api_payload(monkeypatch):
    payload = {'home': {}, 'away': {}, 'teamInfo': {}}
    monkeypatch.setattr(get_scores.statsapi, 'boxscore_data', lambda pk: payload)
    assert get_scores.box_score(745458) is payload


def test_box_score_returns_partial_payload_without_raising(monkeypatch):
    monkeypatch.setattr(get_scores.statsapi, 'boxscore_data', lambda pk: {'teamInfo': {}})
    assert get_scores.box_score(745458) == {'teamInfo': {}}


def test_get_play_by_play_unwraps_live_data(monkeypatch):
    plays = {'allPlays': [{'result': {'event': 'Strikeout'}}]}
    monkeypatch.setattr(get_scores.statsapi, 'get',
                        lambda endpoint, params: {'liveData': {'plays': plays}})
    assert get_scores.get_play_by_play(745458) == plays


def test_get_play_by_play_returns_empty_when_missing(monkeypatch):
    monkeypatch.setattr(get_scores.statsapi, 'get', lambda endpoint, params: {})
    assert get_scores.get_play_by_play(745458) == {}


def test_get_yesterday_returns_empty_on_off_day(monkeypatch):
    monkeypatch.setattr(get_scores, 'games_for_date', lambda target_date: [])
    assert get_scores.get_yesterday() == []


def test_get_yesterday_bundles_each_game(monkeypatch):
    monkeypatch.setattr(get_scores, 'games_for_date', lambda target_date: [_game(1), _game(2)])
    monkeypatch.setattr(get_scores, 'box_score', lambda pk: {'box': pk})
    monkeypatch.setattr(get_scores, 'get_play_by_play', lambda pk: {'plays': pk})

    results = get_scores.get_yesterday()

    assert [r['game']['game_id'] for r in results] == [1, 2]
    assert results[0]['box_score'] == {'box': 1}
    assert results[1]['play_by_play'] == {'plays': 2}
