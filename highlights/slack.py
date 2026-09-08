'''
formats and posts the daily MLB highlights to a slack webhook - if that is set to be true

takes the narrative summary, top lines, and rare events from upstream modules
and turns them into slack block kit blocks. the actual webhook URL comes from
GitHub Actions secrets and gets injected as an env var by the workflow.
'''

import os
import datetime
import requests

from highlights import formatting

# block kit hard limits — if we ever get close to these the message will silently
# be dropped by slack, so we guard against it proactively
_max_blocks = 50
_max_text_characters = 3000


def _truncate(text, limit=_max_text_characters):
    '''slack rejects text over 3000 characters
    cut off with visible indicator so we dont fail'''
    return formatting.truncate(text, limit, '\n_(truncated)_')


def build_blocks(text, top_lines, rare_events):
    '''assemble a list of slack block kit block dicts from the provided data.

    text        — a mrkdwn string with the day's narrative summary
    top_lines   — list of dicts, each with 'player', 'team', 'line' keys
    rare_events — list of dicts, each with 'description' and 'since' keys

    separated from post() so we can unit-test block shape without actually
    hitting slack.
    '''

    # header block — plain_text only, no mrkdwn allowed here by slack's spec
    blocks = [
        {'type': 'header', 'text': {'type': 'plain_text', 'text': 'Yesterday in MLB'}},
    ]

    # the narrative summary goes first — this is the LLM-generated paragraph
    # describing the overall day (or an off-day note if no games were played)
    blocks.append({
        'type': 'section',
        'text': {'type': 'mrkdwn', 'text': _truncate(text)},
    })

    blocks.append({'type': 'divider'})

    if top_lines:
        # bullet each player line: "• *Name* (Team): stat line"
        lines_str = '*Top lines*\n' + '\n'.join(
            f'• *{perf["player"]}* ({perf["team"]}): {perf["line"]}' for perf in top_lines
        )
        blocks.append({
            'type': 'section',
            'text': {'type': 'mrkdwn', 'text': _truncate(lines_str)},
        })
    else:
        # off-day or all postponements — we still post so the channel
        # stays consistent (easier to notice a gap in the archive later)
        blocks.append({
            'type': 'section',
            'text': {'type': 'mrkdwn', 'text': '_No games yesterday._'},
        })

    if rare_events:
        # rare events are things like no-hitters, 4-HR games, cycles, etc.
        events_str = '*Notable firsts / rare events*\n' + '\n'.join(
            f'• {formatting.event_line(event)}' for event in rare_events
        )
        blocks.append({
            'type': 'section',
            'text': {'type': 'mrkdwn', 'text': _truncate(events_str)},
        })

    # context block at the bottom — slack renders this in small gray text,
    # good place for bot attribution and the run timestamp
    ts = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    blocks.append({
        'type': 'context',
        'elements': [
            {
                'type': 'mrkdwn',
                'text': f'MLB Daily Highlights Bot · {ts}',
            }
        ],
    })

    # safety valve — if some future code path packs in too many blocks we clip
    # rather than let the whole request get a 400 from slack
    if len(blocks) > _max_blocks:
        # keep header + context, warn in the middle that we truncated
        blocks = blocks[:_max_blocks - 1]
        blocks.append({
            'type': 'context',
            'elements': [{'type': 'mrkdwn', 'text': '_Some blocks omitted (50-block limit reached)._'}],
        })

    return blocks


def post(text, top_lines, rare_events):
    '''build blocks from the given data and POST them to the main slack webhook.

    will blow up if SLACK_WEBHOOK_URL isn't set, or if slack returns a non-2xx —
    that way the GitHub Actions workflow marks the run as failed and we
    see it in the Actions log instead of silently swallowing it.
    '''
    # re-read from env at call time so the function works even if the env var
    # was injected after the module was first imported
    webhook = os.environ.get('SLACK_WEBHOOK_URL')
    if not webhook:
        raise RuntimeError('SLACK_WEBHOOK_URL env var is not set — cannot post to slack')

    blocks = build_blocks(text, top_lines, rare_events)

    print(f'posting {len(blocks)} blocks to slack webhook...')

    resp = requests.post(webhook, json={'blocks': blocks}, timeout=10)

    # log the status code so it shows up in the Actions run log
    print(f'slack response status: {resp.status_code}')

    # raise immediately so the caller / scheduler sees a failure rather than
    # a quiet success with a malformed message
    resp.raise_for_status()


def post_error(msg):
    '''post a plain error alert to the error channel.

    uses SLACK_ERROR_WEBHOOK_URL — if that env var isn't set we skip silently
    so this can be called unconditionally without crashing local dev runs.
    '''
    # re-read at call time for the same reason as post() above
    error_webhook = os.environ.get('SLACK_ERROR_WEBHOOK_URL')
    if not error_webhook:
        # no error channel configured — print so it still shows up in logs
        print(f'error alert (no error webhook configured): {msg}')
        return

    # keep error payloads simple — just text, no fancy blocks, so they're
    # readable even if the formatting helpers above are what broke things
    payload = {
        'text': f':rotating_light: *MLB Highlights Bot error*\n{msg}',
    }

    print('posting error alert to slack error webhook...')

    try:
        resp = requests.post(error_webhook, json=payload, timeout=10)
        print(f'slack error-channel response status: {resp.status_code}')
        resp.raise_for_status()
    except requests.RequestException as exc:
        # don't let a failure in the error reporter mask the original error —
        # just log and move on
        print(f'failed to post error alert to slack: {exc}')
