'''unit tests for the slack block kit formatter and webhook posting.'''

import pytest
import requests

from highlights import slack


TOP_LINES = [{'player': 'Rafael Devers', 'team': 'Red Sox', 'line': '3-4, 2 HR, 4 RBI'}]
RARE_EVENTS = [
    {'description': 'Cycle by Rafael Devers', 'since': '2019'},
    {'description': 'Immaculate inning', 'since': None},
]


class _FakeResponse:
    def __init__(self, status_code=200, error=None):
        self.status_code = status_code
        self._error = error

    def raise_for_status(self):
        if self._error:
            raise self._error


def _section_texts(blocks):
    return [b['text']['text'] for b in blocks if b['type'] == 'section']


def test_truncate_leaves_short_text_alone():
    assert slack._truncate('short') == 'short'


def test_truncate_marks_long_text():
    out = slack._truncate('x' * 5000)
    assert len(out) <= slack._max_text_characters
    assert out.endswith('_(truncated)_')


def test_build_blocks_shape():
    blocks = slack.build_blocks('narrative', TOP_LINES, RARE_EVENTS)
    assert blocks[0]['type'] == 'header'
    assert blocks[0]['text']['text'] == 'Yesterday in MLB'
    assert blocks[1]['text']['text'] == 'narrative'
    assert blocks[2]['type'] == 'divider'
    assert blocks[-1]['type'] == 'context'


def test_build_blocks_renders_top_lines_and_events():
    texts = _section_texts(slack.build_blocks('narrative', TOP_LINES, RARE_EVENTS))
    assert '• *Rafael Devers* (Red Sox): 3-4, 2 HR, 4 RBI' in texts[1]
    assert '• Cycle by Rafael Devers — _last seen: 2019_' in texts[2]
    assert '• Immaculate inning' in texts[2]


def test_build_blocks_off_day_message():
    texts = _section_texts(slack.build_blocks('nothing doing', [], []))
    assert texts[1] == '_No games yesterday._'


def test_build_blocks_clips_when_over_the_block_limit(monkeypatch):
    monkeypatch.setattr(slack, '_max_blocks', 3)
    blocks = slack.build_blocks('narrative', TOP_LINES, RARE_EVENTS)
    assert len(blocks) == 3
    assert '50-block limit reached' in blocks[-1]['elements'][0]['text']


def test_post_raises_without_webhook(monkeypatch):
    monkeypatch.delenv('SLACK_WEBHOOK_URL', raising=False)
    with pytest.raises(RuntimeError):
        slack.post('narrative', TOP_LINES, RARE_EVENTS)


def test_post_sends_blocks_to_webhook(monkeypatch):
    monkeypatch.setenv('SLACK_WEBHOOK_URL', 'https://hooks.slack.test/abc')
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured['url'] = url
        captured['json'] = json
        captured['timeout'] = timeout
        return _FakeResponse()

    monkeypatch.setattr(slack.requests, 'post', fake_post)
    slack.post('narrative', TOP_LINES, RARE_EVENTS)

    assert captured['url'] == 'https://hooks.slack.test/abc'
    assert captured['timeout'] == 10
    assert captured['json']['blocks'][0]['type'] == 'header'


def test_post_propagates_http_errors(monkeypatch):
    monkeypatch.setenv('SLACK_WEBHOOK_URL', 'https://hooks.slack.test/abc')
    error = requests.HTTPError('400 client error')
    monkeypatch.setattr(
        slack.requests, 'post',
        lambda *a, **k: _FakeResponse(status_code=400, error=error))
    with pytest.raises(requests.HTTPError):
        slack.post('narrative', TOP_LINES, RARE_EVENTS)


def test_post_error_skips_when_no_error_webhook(monkeypatch):
    monkeypatch.delenv('SLACK_ERROR_WEBHOOK_URL', raising=False)

    def boom(*args, **kwargs):
        raise AssertionError('should not post without an error webhook')

    monkeypatch.setattr(slack.requests, 'post', boom)
    slack.post_error('something broke')  # must not raise


def test_post_error_posts_plain_text(monkeypatch):
    monkeypatch.setenv('SLACK_ERROR_WEBHOOK_URL', 'https://hooks.slack.test/err')
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured['json'] = json
        return _FakeResponse()

    monkeypatch.setattr(slack.requests, 'post', fake_post)
    slack.post_error('something broke')
    assert 'something broke' in captured['json']['text']
    assert 'blocks' not in captured['json']


def test_post_error_swallows_request_exceptions(monkeypatch):
    monkeypatch.setenv('SLACK_ERROR_WEBHOOK_URL', 'https://hooks.slack.test/err')

    def fake_post(*args, **kwargs):
        raise requests.ConnectionError('no route to slack')

    monkeypatch.setattr(slack.requests, 'post', fake_post)
    slack.post_error('something broke')  # error reporter must never raise
