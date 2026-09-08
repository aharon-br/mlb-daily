'''regression tests for error propagation across the pipeline.

these lock in the behaviours that used to fail silently:
  1. an MLB API outage looks like an outage, not like an off-day.
  2. an unusable box score / live feed is an error, not empty data.
  3. one bad game degrades the run instead of killing or hiding it.
  4. recap and ledger writes never destroy the previous file.
  5. a skipped rarity tier or an unrequested AI fallback is reported.

run from the repo root with: python -m pytest tests/test_error_handling.py
'''

import datetime
import json

import pytest
import requests

import main
from highlights import get_scores, narrative, rarity, recaps
from highlights import slack as slack_mod


# ---- get_scores: API failures must not look like quiet days -----------------

def test_games_for_date_raises_instead_of_returning_empty(monkeypatch):
    def _boom(**kwargs):
        raise ConnectionError('statsapi unreachable')

    monkeypatch.setattr(get_scores.statsapi, 'schedule', _boom)

    with pytest.raises(get_scores.MLBAPIError):
        get_scores.games_for_date(datetime.date(2026, 4, 30))


def test_games_for_date_empty_schedule_is_not_an_error(monkeypatch):
    monkeypatch.setattr(get_scores.statsapi, 'schedule', lambda **kwargs: [])
    assert get_scores.games_for_date(datetime.date(2026, 1, 5)) == []


def test_box_score_rejects_payload_without_both_sides(monkeypatch):
    monkeypatch.setattr(get_scores.statsapi, 'boxscore_data', lambda pk: {'home': {}})

    with pytest.raises(get_scores.MLBAPIError, match='missing home/away'):
        get_scores.box_score(745458)


def test_play_by_play_rejects_feed_without_plays(monkeypatch):
    monkeypatch.setattr(get_scores.statsapi, 'get', lambda endpoint, params: {'gameData': {}})

    with pytest.raises(get_scores.MLBAPIError, match='no liveData.plays'):
        get_scores.get_play_by_play(745458)


# ---- main: partial failures are survivable but visible ----------------------

def _stub_pipeline(monkeypatch, failing_game_ids=()):
    '''wire main's collaborators to in-memory stubs; return the recorded calls.'''
    games = [
        {'game_id': 1, 'status': 'Final'},
        {'game_id': 2, 'status': 'Final'},
    ]
    recorded = {'recaps': [], 'slack': []}

    def _box(game_pk):
        if game_pk in failing_game_ids:
            raise get_scores.MLBAPIError(f'boxscore_data({game_pk}) failed: 503')
        return {'home': {}, 'away': {}}

    monkeypatch.setattr(main.get_scores, 'games_for_date', lambda target: games)
    monkeypatch.setattr(main.get_scores, 'box_score', _box)
    monkeypatch.setattr(main.get_scores, 'get_play_by_play', lambda pk: {})
    monkeypatch.setattr(main.statlines, 'top_performances', lambda *a, **k: [])
    monkeypatch.setattr(main.rarity, 'find_rare_events', lambda *a, **k: [])
    monkeypatch.setattr(main.narrative, 'write_summary', lambda **kwargs: 'summary')
    monkeypatch.setattr(main.recaps, 'write_recap', lambda **kwargs: recorded['recaps'].append(kwargs))
    monkeypatch.setattr(main.slack_send, 'post', lambda *a: recorded['slack'].append(a))
    monkeypatch.setenv('POST_TO_SLACK', 'true')
    monkeypatch.setenv('AI_SUMMARY', 'false')
    return recorded


def test_one_unloadable_game_still_produces_output_but_fails_the_run(monkeypatch):
    recorded = _stub_pipeline(monkeypatch, failing_game_ids=(2,))

    with pytest.raises(main.PartialRunError) as excinfo:
        main.main()

    assert len(recorded['recaps']) == 1        # recap still written
    assert len(recorded['slack']) == 1         # slack still posted
    assert any('game 2' in reason for reason in excinfo.value.degradations)


def test_all_games_unloadable_is_fatal(monkeypatch):
    recorded = _stub_pipeline(monkeypatch, failing_game_ids=(1, 2))

    with pytest.raises(RuntimeError, match='could not load any'):
        main.main()

    assert recorded['recaps'] == []


def test_recap_is_written_before_a_failing_slack_post(monkeypatch):
    recorded = _stub_pipeline(monkeypatch)

    def _failing_post(*args):
        raise RuntimeError('slack webhook returned 500: server_error')

    monkeypatch.setattr(main.slack_send, 'post', _failing_post)

    with pytest.raises(RuntimeError, match='slack webhook'):
        main.main()

    assert len(recorded['recaps']) == 1


# ---- rarity: skipped tiers are reported, writes are atomic ------------------

def test_missing_corpus_records_a_degradation(monkeypatch):
    monkeypatch.setattr(rarity, '_load_season_ledger', lambda: {})
    monkeypatch.setattr(rarity, '_write_season_ledger', lambda ledger: None)
    monkeypatch.setattr(
        rarity, '_load_pattern_corpus',
        lambda: (_ for _ in ()).throw(FileNotFoundError('no parquet')),
    )

    degradations = []
    rarity.find_rare_events([], datetime.date(2026, 4, 30), degradations=degradations)

    assert any('tier 3' in reason for reason in degradations)


def test_ledger_write_failure_is_reported_not_swallowed(monkeypatch):
    monkeypatch.setattr(rarity, '_load_season_ledger', lambda: {})
    monkeypatch.setattr(rarity, '_load_pattern_corpus', lambda: (_ for _ in ()).throw(FileNotFoundError()))

    def _failing_write(ledger):
        raise OSError('read-only file system')

    monkeypatch.setattr(rarity, '_write_season_ledger', _failing_write)

    degradations = []
    rarity.find_rare_events([], datetime.date(2026, 4, 30), degradations=degradations)

    assert any('ledger not written' in reason for reason in degradations)


def test_corrupt_ledger_raises_with_the_path(monkeypatch, tmp_path):
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    year = datetime.date.today().year
    (data_dir / f'season-ledger-{year}.json').write_text('{"hitting_streaks": ')
    monkeypatch.setattr(rarity, '_repo_root', lambda: str(tmp_path))

    with pytest.raises(ValueError, match='not valid JSON'):
        rarity._load_season_ledger()


def test_ledger_write_leaves_previous_file_intact_on_failure(monkeypatch, tmp_path):
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    year = datetime.date.today().year
    path = data_dir / f'season-ledger-{year}.json'
    path.write_text(json.dumps({'hitting_streaks': {'1': {'streak': 12}}}))
    monkeypatch.setattr(rarity, '_repo_root', lambda: str(tmp_path))

    class _Unserializable:
        # json.dump(default=str) stringifies anything, so fail in __str__
        def __str__(self):
            raise ValueError('cannot serialize')

    with pytest.raises(ValueError):
        rarity._write_season_ledger({'boom': _Unserializable()})

    assert json.loads(path.read_text())['hitting_streaks']['1']['streak'] == 12
    assert [p.name for p in data_dir.iterdir()] == [path.name]


# ---- recaps: never destroy the archive to write one entry -------------------

def test_recap_write_leaves_previous_archive_intact_on_failure(monkeypatch, tmp_path):
    recaps_dir = tmp_path / 'recaps'
    recaps_dir.mkdir()
    archive = recaps_dir / '2026.md'
    archive.write_text('## April 29, 2026\n\nprevious entry\n')
    monkeypatch.setattr(recaps, '_repo_root', lambda: str(tmp_path))

    def _failing_replace(src, dst):
        raise OSError('disk full')

    monkeypatch.setattr(recaps.os, 'replace', _failing_replace)

    with pytest.raises(OSError):
        recaps.write_recap(
            date=datetime.date(2026, 4, 30),
            games=[],
            top_lines=[],
            rare_events=[],
            summary='today',
        )

    assert archive.read_text() == '## April 29, 2026\n\nprevious entry\n'
    assert [p.name for p in recaps_dir.iterdir()] == ['2026.md']


# ---- narrative: a requested-but-missing narrative is a degradation ----------

def test_missing_api_key_with_ai_summary_on_is_reported(monkeypatch):
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    degradations = []

    text = narrative.write_summary(
        date=datetime.date(2026, 4, 30),
        games=[],
        top_lines=[],
        rare_events=[],
        ai_summary=True,
        degradations=degradations,
    )

    assert 'narrative unavailable' in text
    assert any('ANTHROPIC_API_KEY' in reason for reason in degradations)


def test_ai_summary_off_is_not_a_degradation(monkeypatch):
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    degradations = []

    narrative.write_summary(
        date=datetime.date(2026, 4, 30),
        games=[],
        top_lines=[],
        rare_events=[],
        ai_summary=False,
        degradations=degradations,
    )

    assert degradations == []


# ---- slack: failures carry slack's own explanation --------------------------

def test_post_includes_slack_response_body_in_the_error(monkeypatch):
    monkeypatch.setenv('SLACK_WEBHOOK_URL', 'https://hooks.slack.com/services/test')

    class _Resp:
        ok = False
        status_code = 400
        text = 'invalid_blocks'

    monkeypatch.setattr(slack_mod.requests, 'post', lambda *a, **k: _Resp())

    with pytest.raises(RuntimeError, match='invalid_blocks'):
        slack_mod.post('text', [], [])


def test_post_wraps_transport_errors(monkeypatch):
    monkeypatch.setenv('SLACK_WEBHOOK_URL', 'https://hooks.slack.com/services/test')

    def _boom(*args, **kwargs):
        raise requests.ConnectTimeout('timed out')

    monkeypatch.setattr(slack_mod.requests, 'post', _boom)

    with pytest.raises(RuntimeError, match='slack webhook request failed'):
        slack_mod.post('text', [], [])


def test_post_error_never_raises(monkeypatch, capsys):
    monkeypatch.setenv('SLACK_ERROR_WEBHOOK_URL', 'https://hooks.slack.com/services/errors')

    def _boom(*args, **kwargs):
        raise requests.ConnectTimeout('timed out')

    monkeypatch.setattr(slack_mod.requests, 'post', _boom)

    slack_mod.post_error('something broke')

    assert 'failed to post error alert' in capsys.readouterr().out
