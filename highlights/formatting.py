'''
formatting.py — shared text helpers for the output modules.

slack and the recap file both render the same rare-event sentence, and both
slack.py and narrative.py have to keep text under slack's block limits, so the
truncation and the "— last seen: X" suffix live here rather than in each
renderer.
'''


def truncate(text, limit, note, word_boundary=False):
    '''cut text down to `limit` characters, appending `note` when we do.

    the note is counted against the limit so the returned string never exceeds
    it. word_boundary=True backs up to the last space first, so prose doesn't
    end mid-word.
    '''
    if len(text) <= limit:
        return text

    body = text[:limit - len(note)]
    if word_boundary:
        body = body.rsplit(' ', 1)[0]
    return body + note


def event_line(event):
    '''render a rare event as one line of mrkdwn, without any bullet prefix.

    'since' is an ISO date from the tier 3 corpus, or None for tier 1/2 events
    where we have no historical context to attach.
    '''
    description = event.get('description', '')
    since = event.get('since')
    if since:
        return f'{description} — _last seen: {since}_'
    return description
