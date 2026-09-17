'''
narrative.py — asks Claude to write the daily MLB recap blurb.

this is the "voice" layer of the bot. it takes verified facts from upstream
modules and asks the model to turn them into a short, readable narrative suitable
for a slack post. the key constraint is that the model must ONLY work with the
data we hand it — it should never invent stats, records, or "first since" claims
on its own. that's what the verified rare_events list is for: we've already
done the fact-checking, so the model just narrates.

reads ANTHROPIC_API_KEY from the environment (from GitHub secrets in
Actions, or set locally). if AI_SUMMARY env var is "false" or not set, no API
call is made and the fallback plain-text summary is returned instead.
'''

import os
import json

from highlights import formatting


# ---- constants --------------------------------------------------------------

# claude-haiku-4-5 via the anthropic SDK — fast and cheap for a short daily recap.
_MODEL = 'claude-haiku-4-5'

# 1200 tokens is roughly 900 words — plenty for a tight slack recap that covers
# a 2-sentence opener, 3-5 performance bullets, and any rare events. going much
# larger risks blowing slack's ~3000 char text block limit downstream.
_MAX_TOKENS = 1200

# system prompt lives here so it's easy to tune without touching the function.
# the most important rule is the second one: do NOT invent history. the
# rare_events list is pre-verified by rarity.py — claude's job is narration,
# not research. if rare_events is empty, that means nothing unusual happened
# and it should write accordingly (no manufactured drama).
_SYSTEM = '''You are a baseball writer summarizing yesterday's MLB action for a Slack channel.

Rules:
- Use ONLY the facts in the provided JSON. Do not invent stats, dates, or "first since" claims.
- If the rare_events list is empty, do not manufacture history.
- Keep it tight: a 2-sentence opener, then the top 3-5 performances, then the rare events.
- Tone: knowledgeable fan, not breathless. No emoji unless the data is genuinely wild.'''

# slack's text block hard limit is 3000 chars. we leave a 200 char buffer for
# any formatting the slack module adds around the narrative text itself.
_SLACK_SAFE_CHAR_LIMIT = 2800


# ---- helpers ----------------------------------------------------------------

def _payload(date, games, top_lines, rare_events):
    '''pack everything the model needs into a single dict for the user message.

    keeping this as a plain dict (rather than a pre-formatted string) means
    the model gets clean, unambiguous structure to work from. json is easier to
    parse accurately than free-form text mixed with stats.
    '''
    return {
        'date': date.isoformat(),       # 'YYYY-MM-DD' — unambiguous calendar date
        'game_count': len(games),        # gives the model the scale of the day's slate
        'top_lines': top_lines,          # list of dicts from statlines.top_performances
        'rare_events': rare_events,      # already verified — model just narrates these
    }


def _truncate(text):
    '''trim the narrative if it's going to blow slack's text block limit.

    this should almost never trigger at max_tokens=1200, but it's a safety net
    for the occasional long-winded response. we add a note so the slack post
    doesn't just end mid-sentence and confuse readers.
    '''
    # word_boundary so we don't slice a word in half
    return formatting.truncate(
        text,
        _SLACK_SAFE_CHAR_LIMIT,
        '... *(truncated for Slack)*',
        word_boundary=True,
    )


def _fallback(date, games, top_lines, rare_events):
    '''plain-text fallback if the claude call fails entirely or ai_summary is off.
    when the api is unavailable (network error, quota, missing key), we still
    want the post to go out — just without prose. this gives a
    bunch of data that's better than silence. 
    it's pretty ugly 
    if you're seeing this after enabling ai_summary,
    something is wrong with the anthropic integration
    '''
    lines = [f'*MLB Recap — {date.isoformat()}* (narrative unavailable)']
    lines.append(f'Games played: {len(games)}')

    if top_lines:
        lines.append('\n*Top performances:*')
        for perf in top_lines[:5]:  # cap at 5 so the fallback stays scannable
            name = perf.get('player', 'Unknown')
            line = perf.get('line', '')
            category = perf.get('category', '')
            team = perf.get('team', '')
            summary = f'({team}) [{category}]: {line}' if team else line
            lines.append(f'• {name}: {summary}')

    if rare_events:
        lines.append('\n*Notable events:*')
        for event in rare_events:
            label = event.get('label', str(event))
            lines.append(f'• {label}')

    return '\n'.join(lines)


# ---- main entry point -------------------------------------------------------

def write_summary(date, games, top_lines, rare_events, ai_summary=False):
    '''generate the daily recap narrative, optionally via the claude api.

    the anthropic client is instantiated lazily inside this function — so
    importing highlights.narrative won't blow up if ANTHROPIC_API_KEY isn't
    set yet (handy during local unit tests that mock this function). you only
    pay the import cost when we actually make a call.

    date        — datetime.date for the day being recapped (yesterday)
    games       — list of game dicts from get_scores.games_for_date (for count)
    top_lines   — list of stat-line dicts from statlines.top_performances
    rare_events — list of verified rare-event dicts from rarity.find_rare_events
    ai_summary  — if False (default), skip the api call and return the fallback

    returns a str — the narrative text, ready to embed in a slack message.
    if ai_summary is False or ANTHROPIC_API_KEY is not set, returns a plain
    bullet-point fallback. if the claude call fails, also falls back gracefully.
    '''

    api_key = os.environ.get('ANTHROPIC_API_KEY')

    if not ai_summary or not api_key:
        if ai_summary and not api_key:
            print('warning: ai_summary=True but ANTHROPIC_API_KEY not set — using fallback')
        return _fallback(date, games, top_lines, rare_events)

    try:
        import anthropic
    except ImportError:
        print('error: anthropic not installed — pip install anthropic')
        return _fallback(date, games, top_lines, rare_events)

    payload = _payload(date, games, top_lines, rare_events)
    payload_json = json.dumps(payload, sort_keys=True, default=str)

    print(f'calling claude api with model {_MODEL}...')

    try:
        client = anthropic.Anthropic(api_key=api_key)

        response = client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM,
            messages=[{'role': 'user', 'content': f'Write the recap.\n\n{payload_json}'}],
        )

        text = response.content[0].text

        if not text:
            print('warning: model returned no text content, using fallback')
            return _fallback(date, games, top_lines, rare_events)

        print(f'narrative generated ({len(text)} chars)')
        return _truncate(text)

    except Exception as exc:
        print(f'claude api call failed: {exc}')
        print('falling back to plain text summary')
        return _fallback(date, games, top_lines, rare_events)
