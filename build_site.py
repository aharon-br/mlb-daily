'''
build_site.py — turns recaps/*.md into a static site in public/

Vercel runs this on every deploy (see vercel.json). Standard library only, so there is
nothing to install. GitHub Actions commits a new recap each morning, which triggers the deploy.

-- python build_site.py     writes public/index.html, public/{YYYY-MM-DD}/index.html, public/style.css
'''

import glob
import html
import os
import re
import shutil
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, 'public')
SITE = os.path.join(ROOT, 'site')

FONTS = ('https://fonts.googleapis.com/css2?family=Big+Shoulders+Display:wght@700;800'
         '&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap')


# ---- parsing

def parse_recaps():
    '''read every recaps/{year}.md and return entries newest-first.

    each entry: date, games, summary, lines [(player, team, line)], events [(text, since)]
    '''
    entries = []
    for path in glob.glob(os.path.join(ROOT, 'recaps', '*.md')):
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
        for chunk in re.split(r'^---\s*$', text, flags=re.M):
            entry = _parse_entry(chunk)
            if entry:
                entries.append(entry)
    entries.sort(key=lambda e: e['date'], reverse=True)
    return entries


def _parse_entry(chunk):
    m = re.search(r'^## (.+)$', chunk, flags=re.M)
    if not m:
        return None
    try:
        date = datetime.strptime(m.group(1).strip(), '%B %d, %Y').date()
    except ValueError:
        return None

    games = re.search(r'_(\d+) games? played_', chunk)
    body = chunk[m.end():]

    # summary sits between the games line and the **Top lines** heading
    head, _, rest = body.partition('**Top lines**')
    summary = head.split('games played_', 1)[-1].strip()
    # the fallback text the bot writes when the AI step is off is not worth showing
    if 'narrative unavailable' in summary:
        summary = ''

    lines_part, _, events_part = rest.partition('**Notable events**')
    lines = []
    for lm in re.finditer(r'^- \*\*(.+?)\*\* \((.+?)\): (.+)$', lines_part, flags=re.M):
        lines.append(lm.groups())

    events = []
    for em in re.finditer(r'^- (.+?)(?: — _last seen: (.+?)_)?$', events_part, flags=re.M):
        events.append((em.group(1), em.group(2)))

    return {
        'date': date,
        'games': int(games.group(1)) if games else 0,
        'summary': summary,
        'lines': lines,
        'events': events,
    }


# ---- rendering

def esc(s):
    return html.escape(s, quote=True)


def long_date(d):
    return d.strftime('%B %-d, %Y')


def short_date(d):
    return d.strftime('%b %-d, %Y')


def games_label(n):
    return f'{n} game' if n == 1 else f'{n} games'


def stat_html(line):
    '''wrap the headline numbers (home runs, wins, saves) so they stand out.'''
    out = esc(line)
    return re.sub(r'\b(\d+ HR|W|SV)\b', r'<span class="hl">\1</span>', out)


def page(title, body, depth=0):
    prefix = '../' * depth
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<link rel="stylesheet" href="{FONTS}">
<link rel="stylesheet" href="{prefix}style.css">
</head>
<body>
<div class="wrap">
  <header class="mast">
    <h1><a href="{prefix or './'}">MLB <span>Daily</span></a></h1>
    <p>Yesterday's best stat lines, every morning at 9 ET.</p>
  </header>
{body}
  <footer>
    <span>Data from the MLB Stats API.</span>
    <a href="https://github.com/aharon-br/mlb-daily">Source on GitHub</a>
  </footer>
</div>
</body>
</html>
'''


def recap_html(e, eyebrow):
    rows = []
    for i, (player, team, line) in enumerate(e['lines']):
        tag = ('pit', 'PIT') if ' IP' in line else ('bat', 'BAT')
        lead = ' lead' if i == 0 else ''
        rows.append(
            f'<div class="row{lead}" role="listitem"><span class="tag {tag[0]}">{tag[1]}</span>'
            f'<div class="who"><b>{esc(player)}</b><span>{esc(team)}</span></div>'
            f'<div class="line">{stat_html(line)}</div></div>')

    box = f'<div class="box" role="list">{"".join(rows)}</div>' if rows else ''
    story = f'<p class="story">{esc(e["summary"])}</p>' if e['summary'] else ''

    notable = ''
    if e['events']:
        items = ''.join(
            f'<li><span>{esc(text)}</span>' + (f'<small>last seen {esc(since)}</small>' if since else '') + '</li>'
            for text, since in e['events'])
        notable = f'<div class="notable"><h3>Notable events</h3><ul>{items}</ul></div>'

    return f'''  <main>
    <article class="recap">
      <div class="recap-head">
        <div>
          <div class="eyebrow">{esc(eyebrow)}</div>
          <h2>{long_date(e['date'])}</h2>
        </div>
        <span class="count">{games_label(e['games'])} played</span>
      </div>
      {story}
      {notable}
      {box}
    </article>
  </main>'''


def archive_html(entries, prefix=''):
    items = []
    for e in entries:
        top = e['lines'][0] if e['lines'] else None
        teaser = f'<b>{esc(top[0])}</b> {esc(top[2])}' if top else ''
        n = f'<span class="n">{len(e["events"])} notable</span>' if e['events'] else ''
        items.append(
            f'<li><a href="{prefix}{e["date"].isoformat()}/">'
            f'<span class="d">{short_date(e["date"])}</span>'
            f'<span class="g">{games_label(e["games"])}</span>'
            f'<span class="t">{teaser}{n}</span></a></li>')
    return f'''  <section class="archive">
    <h3>Archive</h3>
    <ul class="ledger">{"".join(items)}</ul>
  </section>'''


def pager_html(entries, i):
    # entries are newest-first, so "older" is the next index
    older = entries[i + 1] if i + 1 < len(entries) else None
    newer = entries[i - 1] if i > 0 else None
    left = f'<a href="../{older["date"].isoformat()}/">&larr; {short_date(older["date"])}</a>' if older else '<span></span>'
    right = f'<a href="../{newer["date"].isoformat()}/">{short_date(newer["date"])} &rarr;</a>' if newer else '<span></span>'
    return f'  <nav class="pager" aria-label="Recap pages">{left}<a href="../">All recaps</a>{right}</nav>'


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(content)


def main():
    entries = parse_recaps()
    if not entries:
        raise SystemExit('no recaps found in recaps/ — nothing to build')

    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(OUT)
    shutil.copy(os.path.join(SITE, 'style.css'), os.path.join(OUT, 'style.css'))

    latest = entries[0]
    write(os.path.join(OUT, 'index.html'),
          page('MLB Daily', recap_html(latest, 'Latest recap') + '\n' + archive_html(entries)))

    for i, e in enumerate(entries):
        title = f'MLB Daily, {long_date(e["date"])}'
        body = recap_html(e, 'Recap') + '\n' + pager_html(entries, i)
        write(os.path.join(OUT, e['date'].isoformat(), 'index.html'), page(title, body, depth=1))

    print(f'built {len(entries)} recap pages into public/ (latest: {latest["date"]})')


if __name__ == '__main__':
    main()
