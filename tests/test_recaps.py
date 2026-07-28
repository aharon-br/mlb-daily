'''unit tests for the markdown recap writer.'''

import datetime
import os

from highlights import recaps


DATE = datetime.date(2026, 4, 30)
GAMES = [{'game_id': 1}, {'game_id': 2}]
TOP_LINES = [{'player': 'Rafael Devers', 'team': 'Red Sox', 'line': '3-4, 2 HR, 4 RBI'}]
RARE_EVENTS = [
    {'description': 'Cycle by Rafael Devers', 'since': '2019'},
    {'description': 'Immaculate inning', 'since': None},
]


def _use_tmp_repo(monkeypatch, tmp_path):
    '''point the module at a throwaway repo root (it derives it from __file__).'''
    fake_module_path = tmp_path / 'highlights' / 'recaps.py'
    monkeypatch.setattr(recaps, '__file__', str(fake_module_path))
    return tmp_path / 'recaps' / '2026.md'


def test_build_entry_includes_all_sections():
    entry = recaps._build_entry(DATE, GAMES, TOP_LINES, RARE_EVENTS, 'the narrative')
    assert entry.startswith('## April 30, 2026\n')
    assert '_2 games played_' in entry
    assert 'the narrative' in entry
    assert '- **Rafael Devers** (Red Sox): 3-4, 2 HR, 4 RBI' in entry
    assert '- Cycle by Rafael Devers — _last seen: 2019_' in entry
    assert '- Immaculate inning\n' in entry
    assert entry.endswith('---\n')


def test_build_entry_omits_empty_sections():
    entry = recaps._build_entry(DATE, [], [], [], 'off day')
    assert '_0 games played_' in entry
    assert 'Top lines' not in entry
    assert 'Notable events' not in entry


def test_write_recap_creates_file(monkeypatch, tmp_path):
    filepath = _use_tmp_repo(monkeypatch, tmp_path)
    recaps.write_recap(DATE, GAMES, TOP_LINES, RARE_EVENTS, 'the narrative')
    assert os.path.exists(filepath)
    assert '## April 30, 2026' in filepath.read_text()


def test_write_recap_prepends_newest_entry(monkeypatch, tmp_path):
    filepath = _use_tmp_repo(monkeypatch, tmp_path)
    recaps.write_recap(DATE, GAMES, TOP_LINES, RARE_EVENTS, 'older day')
    recaps.write_recap(datetime.date(2026, 5, 1), GAMES, TOP_LINES, RARE_EVENTS,
                       'newer day')

    content = filepath.read_text()
    assert content.index('May 1, 2026') < content.index('April 30, 2026')


def test_write_recap_uses_one_file_per_year(monkeypatch, tmp_path):
    _use_tmp_repo(monkeypatch, tmp_path)
    recaps.write_recap(DATE, GAMES, TOP_LINES, RARE_EVENTS, 'summary')
    recaps.write_recap(datetime.date(2027, 4, 1), GAMES, TOP_LINES, RARE_EVENTS,
                       'summary')
    assert sorted(p.name for p in (tmp_path / 'recaps').iterdir()) == \
        ['2026.md', '2027.md']
