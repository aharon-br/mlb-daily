'''
writes each day's run to recaps/{year}.md in the repo

new entries are prepended so the file reads newest-first. 
the workflow commits this file alongside the season ledger after every run, 
so the recaps/ folder becomes a browsable archive of every day the bot ran
'''

import os
import re
from datetime import datetime


def write_recap(date, games, top_lines, rare_events, summary, recaps_dir=None):
    '''write a day's run output to recaps/{year}.md, newest entry first.

    date        — datetime.date for the day being recapped
    games       — list of game dicts
    top_lines   — list of stat-line dicts from statlines.top_performances
    rare_events — list of rare-event dicts from rarity.find_rare_events
    summary     — narrative string from narrative.write_summary if we are using AI summary writer
    recaps_dir  — where recaps/{year}.md lives (defaults to the repo's recaps/ folder)
    '''
    if recaps_dir is None:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        recaps_dir = os.path.join(repo_root, 'recaps')
    os.makedirs(recaps_dir, exist_ok=True) 
    # make the recaps dir if its not there, this really only happens on the first run
    # but in case its been deleted or somethig it will start new
    
    year = date.year
    filepath = os.path.join(recaps_dir, f'{year}.md')

    # read existing file so we can prepend today's entry above it
    if os.path.exists(filepath):
        with open(filepath, 'r') as fh:
            existing = fh.read()
    else:
        existing = ''

    entry = _build_entry(date, games, top_lines, rare_events, summary)

    # keep entries newest-first by date. an entry for the same date is replaced, so a
    # rerun or backfill never duplicates a day or lands out of order
    chunks = [chunk for chunk in re.split(r'(?m)^(?=## )', existing) if chunk.strip()]
    kept = [chunk for chunk in chunks if _entry_date(chunk) != date]
    position = len(kept)
    for i, chunk in enumerate(kept):
        chunk_date = _entry_date(chunk)
        if chunk_date is not None and chunk_date < date:
            position = i
            break
    kept.insert(position, entry)

    with open(filepath, 'w') as fh:
        fh.write(''.join(kept))

    print(f'recap written to recaps/{year}.md')


def _entry_date(chunk):
    '''the date from an entry's "## Month D, YYYY" heading, or None if it doesn't parse.'''
    match = re.match(r'## (.+)', chunk)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1).strip(), '%B %d, %Y').date()
    except ValueError:
        return None


def _build_entry(date, games, top_lines, rare_events, summary):
    '''build the markdown format for the day's recap

    format sample:
      ## April 30, 2026

      _15 games played_

      {LLM narrative or fallback text if not using AI}

      **Top lines**
      - **Player Name** (Team): stat line

      **Notable events**
      - event description — _last seen: date_

      ---

    top lines and notable events sections are omitted entirely if empty
    that should be pretty rare not to have at least some top lines, otherwise its a real bad day in baseball
    '''
    date_str = date.strftime('%B %-d, %Y')
    
    # create the lines empty array and keep appending the details that make sense to add
    lines = []
    lines.append(f'## {date_str}')
    lines.append('')
    lines.append(f'_{len(games)} games played_')
    lines.append('')
    lines.append(summary)
    lines.append('')

    if top_lines:
        lines.append('**Top lines**')
        for perf in top_lines:
            lines.append(f'- **{perf["player"]}** ({perf["team"]}): {perf["line"]}')
        lines.append('')

    if rare_events:
        lines.append('**Notable events**')
        for event in rare_events:
            if event.get('since') is not None:
                lines.append(f'- {event["description"]} — _last seen: {event["since"]}_')
            else:
                lines.append(f'- {event["description"]}')
        lines.append('')

    # trailing separator so entries stack cleanly when prepended
    lines.append('---')
    lines.append('')

    return '\n'.join(lines)
