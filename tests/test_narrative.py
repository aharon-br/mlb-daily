'''unit tests for the claude narrative layer.

the anthropic client is never hit — we inject a fake module so the tests cover
the payload construction, the fallback paths, and the truncation guard.
'''

import datetime
import sys
import types

from highlights import narrative


DATE = datetime.date(2026, 4, 30)
GAMES = [{'game_id': 1}, {'game_id': 2}]
TOP_LINES = [
    {'player': 'Rafael Devers', 'team': 'Red Sox', 'category': 'batting',
     'line': '3-4, 2 HR, 4 RBI'},
]
RARE_EVENTS = [{'label': 'Cycle by Rafael Devers'}]


class _FakeMessages:
    def __init__(self, text=None, error=None):
        self._text = text
        self._error = error
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self._error:
            raise self._error
        block = types.SimpleNamespace(text=self._text)
        return types.SimpleNamespace(content=[block])


def _install_fake_anthropic(monkeypatch, text=None, error=None):
    messages = _FakeMessages(text=text, error=error)

    class _FakeClient:
        def __init__(self, api_key=None):
            self.api_key = api_key
            self.messages = messages

    module = types.ModuleType('anthropic')
    module.Anthropic = _FakeClient
    monkeypatch.setitem(sys.modules, 'anthropic', module)
    return messages


def test_payload_shape():
    payload = narrative._payload(DATE, GAMES, TOP_LINES, RARE_EVENTS)
    assert payload == {
        'date': '2026-04-30',
        'game_count': 2,
        'top_lines': TOP_LINES,
        'rare_events': RARE_EVENTS,
    }


def test_truncate_leaves_short_text_alone():
    assert narrative._truncate('short recap') == 'short recap'


def test_truncate_cuts_on_a_word_boundary():
    out = narrative._truncate('word ' * 2000)
    assert out.endswith('... *(truncated for Slack)*')
    assert len(out) <= narrative._SLACK_SAFE_CHAR_LIMIT + len('... *(truncated for Slack)*')


def test_fallback_contains_games_lines_and_events():
    out = narrative._fallback(DATE, GAMES, TOP_LINES, RARE_EVENTS)
    assert '*MLB Recap — 2026-04-30*' in out
    assert 'Games played: 2' in out
    assert '• Rafael Devers: (Red Sox) [batting]: 3-4, 2 HR, 4 RBI' in out
    assert '• Cycle by Rafael Devers' in out


def test_fallback_caps_top_lines_at_five():
    many = [{'player': f'P{i}', 'line': '1-1'} for i in range(9)]
    out = narrative._fallback(DATE, GAMES, many, [])
    assert out.count('•') == 5


def test_fallback_omits_sections_when_empty():
    out = narrative._fallback(DATE, [], [], [])
    assert 'Top performances' not in out
    assert 'Notable events' not in out


def test_write_summary_returns_fallback_when_ai_disabled(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant-test')
    out = narrative.write_summary(DATE, GAMES, TOP_LINES, RARE_EVENTS, ai_summary=False)
    assert 'narrative unavailable' in out


def test_write_summary_returns_fallback_without_api_key(monkeypatch):
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    out = narrative.write_summary(DATE, GAMES, TOP_LINES, RARE_EVENTS, ai_summary=True)
    assert 'narrative unavailable' in out


def test_write_summary_calls_claude(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant-test')
    messages = _install_fake_anthropic(monkeypatch, text='Devers went off.')

    out = narrative.write_summary(DATE, GAMES, TOP_LINES, RARE_EVENTS, ai_summary=True)

    assert out == 'Devers went off.'
    assert messages.kwargs['model'] == narrative._MODEL
    assert messages.kwargs['max_tokens'] == narrative._MAX_TOKENS
    assert messages.kwargs['system'] == narrative._SYSTEM
    assert '2026-04-30' in messages.kwargs['messages'][0]['content']


def test_write_summary_truncates_long_model_output(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant-test')
    _install_fake_anthropic(monkeypatch, text='word ' * 2000)
    out = narrative.write_summary(DATE, GAMES, TOP_LINES, RARE_EVENTS, ai_summary=True)
    assert out.endswith('... *(truncated for Slack)*')


def test_write_summary_falls_back_on_empty_model_output(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant-test')
    _install_fake_anthropic(monkeypatch, text='')
    out = narrative.write_summary(DATE, GAMES, TOP_LINES, RARE_EVENTS, ai_summary=True)
    assert 'narrative unavailable' in out


def test_write_summary_falls_back_on_api_error(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant-test')
    _install_fake_anthropic(monkeypatch, error=RuntimeError('rate limited'))
    out = narrative.write_summary(DATE, GAMES, TOP_LINES, RARE_EVENTS, ai_summary=True)
    assert 'narrative unavailable' in out
