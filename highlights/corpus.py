'''
corpus.py — one-time backfill + daily incremental update for the pattern index.

retrosheet event data goes all the way back to 1918 (though pre-WWII years are
patchy — missing games, missing plays, incomplete pitch sequences). we scan
every play of every game looking for a handful of "interesting" patterns and
write one row per detected occurrence to a parquet file, which you commit to
the repo. the bot then queries the parquet to answer questions like "how often
does a walk-off grand slam happen?" or "who threw the last immaculate inning?"

why parquet? because we're scanning millions of rows once and reading a small
subset many times. parquet's columnar storage and predicate pushdown make
queries like "all walk_off_grand_slams after 2000" nearly instant compared to
scanning a csv.

why retrosheet and not the MLB stats API? the stats API only reliably goes
back to about 2008 for play-by-play. retrosheet has event files going back a
century, which is what makes the historical rarity framing interesting.

data source: Chadwick Bureau's retrosheet mirror on GitHub
  https://github.com/chadwickbureau/retrosheet/tree/master/seasons/

parsing: the Chadwick tools (https://chadwick.readthedocs.io/) must be
installed before running this script:
  brew install chadwick   # macOS (Homebrew — installs current 0.10.x)
  # Linux: apt's chadwick package is 0.6.x (~2012) and will not work correctly.
  # Install from GitHub releases instead (see update-corpus.yml for the one-liner).

the retrosheet event file format is described at:
  https://www.retrosheet.org/eventfile.htm

cwevent column reference (used with -n flag for named headers):
  game_id         — unique game ID, e.g. "ANA200304080"
  bat_home_id     — 0 if visiting team bats, 1 if home team bats
  inn_ct          — inning number (1-indexed)
  event_cd        — numeric event type (23=HR, 3=K, etc.)
  bat_event_fl    — T if this row is a true batting event
  bat_id          — batter's retrosheet player ID
  pit_id          — pitcher's retrosheet player ID
  base1_run_id    — runner on first (empty string = nobody)
  base2_run_id    — runner on second
  base3_run_id    — runner on third
  home_score_ct   — home team's cumulative score after this play
  away_score_ct   — visiting team's cumulative score after this play
  pitch_seq_tx    — pitch-by-pitch sequence string (S=swinging K, C=called K, etc.)
  pit_start_fl    — T if pitcher is the game starter (extended field)

  note: HOME_TEAM_ID and AWAY_TEAM_ID are derived from game_id and the
  "visiting team" field that cwevent outputs.
'''

import os
import subprocess
import tempfile
import requests
import pandas as pd
from io import StringIO


# ---- cwevent field list -------------------------------------------------
# cwevent default fields: 0-6,8-9,12-13,16-17,26-40,43-45,51,58-61
# we add field 7 (pitch sequence) which is not in the default set.
# extended fields (-x): 15 = vis team id, 16 = home team id, 19 = pitcher is starter flag.
# we use -n to get named column headers.
#
# cwevent field numbers used:
#   0  = game_id
#   1  = visiting_team_id  (cwevent calls it "visiting team" in -d output)
#   2  = inn_ct
#   3  = bat_home_id
#   7  = pitch_seq_tx  (NOT in default -f, must be added)
#   8  = vis_score_ct  (away score)
#   9  = home_score_ct
#   10 = bat_id
#   12 = res_bat_id     (responsible batter — same as bat_id except rare PR situations)
#   14 = pit_id
#   16 = res_pit_id
#   26 = base1_run_id
#   27 = base2_run_id
#   28 = base3_run_id
#   34 = event_cd
#   35 = bat_event_fl
# extended field 19 = pit_start_fl  (pitcher is starter flag)
#
# the home team ID is embedded in game_id (first 3 chars).

CWEVENT_FIELDS = '0-9,10,12,14,16,26-40,43-45,51,58-61'
CWEVENT_EXTENDED = '19'   # pitcher is starter flag

# ---- retrosheet EVENT_CD codes we care about --------------------------------
EVENT_HR = 23          # home run (includes inside-the-park)
EVENT_STRIKEOUT = 3    # strikeout


# ---- GitHub raw file URL base for Chadwick Bureau retrosheet mirror -----
_GH_RAW_BASE = 'https://raw.githubusercontent.com/chadwickbureau/retrosheet/master/seasons'
_GH_API_BASE = 'https://api.github.com/repos/chadwickbureau/retrosheet/contents/seasons'


def _download_year_to_dir(year, tmpdir):
    '''download all event files (.EVA, .EVN, .EVE), roster files (.ROS),
    and the TEAM file for a given year into tmpdir.

    returns the list of event file paths written, or an empty list if the
    year has no data.
    '''
    gh_token = os.environ.get('GH_TOKEN', '')
    headers = {'Authorization': f'token {gh_token}'} if gh_token else {}

    url = f'{_GH_API_BASE}/{year}'
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code == 404:
        return []  # year doesn't exist in repo
    resp.raise_for_status()

    contents = resp.json()
    if not isinstance(contents, list):
        return []

    event_paths = []
    for item in contents:
        name = item['name']
        ext = name.rsplit('.', 1)[-1].upper() if '.' in name else ''

        # .ROS (roster) and TEAM files are REQUIRED by cwevent — not optional.
        # cwevent looks them up at parse time to resolve player names and
        # league context. without them in the same directory as the event
        # file, cwevent will silently produce empty or incorrect output.
        if ext in ('EVA', 'EVN', 'EVE', 'ROS') or name.upper().startswith('TEAM'):
            raw_url = f'{_GH_RAW_BASE}/{year}/{name}'
            file_resp = requests.get(raw_url, headers=headers, timeout=60)
            if file_resp.status_code != 200:
                continue
            dest = os.path.join(tmpdir, name)
            with open(dest, 'w', encoding='utf-8', errors='replace') as file:
                file.write(file_resp.text)
            if ext in ('EVA', 'EVN', 'EVE'):
                event_paths.append(dest)

    return event_paths


def _run_cwevent(event_paths, year, tmpdir):
    '''run cwevent one file at a time and concatenate results.

    passing all files at once causes cwevent to hang on large seasons.
    processing per-file with a 60s timeout keeps things moving.

    returns None if cwevent produces no usable output.
    '''
    if not event_paths:
        return None

    dfs = []
    for path in event_paths:
        cmd = [
            'cwevent',
            '-y', str(year),   # year sets league context (DH rule, infield fly, etc.)
            '-n',              # output named column headers instead of positional numbers
            '-f', CWEVENT_FIELDS,    # standard field selection (different namespace from -x)
            '-x', CWEVENT_EXTENDED,  # extended field selection (pitcher-is-starter flag)
            '-q',              # suppress cwevent's noisy per-row warnings to stderr
            path,
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=tmpdir,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            print(f'  cwevent timed out on {os.path.basename(path)}, skipping')
            continue
        except FileNotFoundError:
            raise RuntimeError(
                'cwevent not found — install the Chadwick tools: brew install chadwick'
            )

        if not result.stdout.strip():
            continue

        try:
            df = pd.read_csv(StringIO(result.stdout), low_memory=False)
            df.columns = [col.strip().upper().replace(' ', '_') for col in df.columns]
            dfs.append(df)
        except Exception as exc:
            print(f'  could not parse cwevent output for {os.path.basename(path)}: {exc}')
            continue

    if not dfs:
        return None

    return pd.concat(dfs, ignore_index=True)


def _normalize_columns(df):
    '''map cwevent's output column names to the names corpus.py expects.

    cwevent -n uses full descriptive names that don't match the retrosheet
    spec abbreviations the rest of corpus.py uses. this function renames them.
    '''
    # cwevent -n outputs full English column names (e.g. "VISITING_TEAM") while
    # the rest of corpus.py uses the Retrosheet spec abbreviations (e.g.
    # "AWAY_TEAM_ID"). this dict maps between the two.
    renames = {
        'GAME_ID':        'GAME_ID',       # already correct
        'VISITING_TEAM':  'AWAY_TEAM_ID',
        'INN_CT':         'INN_CT',         # already correct
        'BATTING_TEAM':   'BAT_HOME_ID',
        'PITCH_SEQUENCE': 'PITCH_SEQ_TX',
        'VIS_SCORE':      'AWAY_SCORE_CT',
        'HOME_SCORE':     'HOME_SCORE_CT',
        'BATTER':         'BAT_ID',
        'RES_BATTER':     'RES_BAT_ID',
        'PITCHER':        'PIT_ID',
        'RES_PITCHER':    'RES_PIT_ID',
        'FIRST_RUNNER':   'BASE1_RUN_ID',
        'SECOND_RUNNER':  'BASE2_RUN_ID',
        'THIRD_RUNNER':   'BASE3_RUN_ID',
        'EVENT_TYPE':     'EVENT_CD',
        'BATTER_EVENT_FLAG': 'BAT_EVENT_FL',
        'PITCHER_IS_STARTER_FLAG': 'PIT_START_FL',
    }

    # rename whatever matches; ignore columns not in the map
    df = df.rename(columns={k: v for k, v in renames.items() if k in df.columns})

    # derive HOME_TEAM_ID from GAME_ID (first 3 chars of the game ID string)
    if 'GAME_ID' in df.columns and 'HOME_TEAM_ID' not in df.columns:
        df['HOME_TEAM_ID'] = df['GAME_ID'].str[:3]

    return df


def _load_events_for_year(year):
    '''download retrosheet event files for year, parse with cwevent, return DataFrame.

    uses a temp directory so all the files land in one place. the temp dir is
    cleaned up automatically.

    returns None if there's no data or something goes wrong.
    '''
    with tempfile.TemporaryDirectory(prefix=f'retrosheet_{year}_') as tmpdir:
        print(f'  downloading {year} event files...', flush=True)
        event_paths = _download_year_to_dir(year, tmpdir)
        if not event_paths:
            return None

        print(f'  parsing {len(event_paths)} event file(s) with cwevent...', flush=True)
        df = _run_cwevent(event_paths, year, tmpdir)
        if df is None:
            return None

        df = _normalize_columns(df)
        return df


def build_pattern_corpus(start_year=1918, end_year=2024, out_path='retrosheet-patterns.parquet'):
    '''one-time backfill: scan all retrosheet event data from start_year through
    end_year, detect interesting patterns, write to parquet locally.

    requires the Chadwick tools to be installed (brew install chadwick).
    downloads event files directly from the Chadwick Bureau GitHub repo —
    no pybaseball needed.

    out_path — where to write the final parquet file locally; commit it to the repo
               to make it available to the daily workflow.
    '''
    all_rows = []

    for year in range(start_year, end_year + 1):
        print(f'processing year {year}...', flush=True)

        try:
            events = _load_events_for_year(year)
        except Exception as exc:
            print(f'  skipping {year} — could not load events: {exc}', flush=True)
            continue

        if events is None or events.empty:
            print(f'  skipping {year} — no event data returned', flush=True)
            continue

        rows = _extract_patterns_from_events(events, year)
        all_rows.extend(rows)
        print(f'  {year}: {len(rows)} pattern occurrence(s) found', flush=True)

    if not all_rows:
        print('warning: no pattern occurrences found across all years — check column names')
        return

    df = pd.DataFrame(all_rows)
    df.to_parquet(out_path, index=False)
    print(f'wrote {len(df)} rows to {out_path}')


def update_corpus_for_year(year, out_path='retrosheet-patterns.parquet'):
    '''incremental update: called each day (or once per season) to append the
    current year's patterns to the existing parquet.

    strategy: re-scan the whole current year rather than trying to diff —
    retrosheet event files for in-progress seasons get updated throughout the
    year, so the simplest correct approach is to replace the current-year rows
    entirely rather than appending. for most seasons the parquet is small
    enough that this is fast.

    year     — the int year to update (almost always the current calendar year)
    out_path — path to the existing parquet file to update
    '''
    print(f'updating corpus for {year}...')

    try:
        events = _load_events_for_year(year)
    except Exception as exc:
        print(f'could not load {year} events: {exc}')
        return

    if events is None or events.empty:
        print(f'no event data for {year} — nothing to update')
        return

    new_rows = _extract_patterns_from_events(events, year)
    print(f'found {len(new_rows)} pattern occurrence(s) for {year}')

    if not new_rows:
        return

    new_df = pd.DataFrame(new_rows)

    # if the parquet already exists, load it and drop any rows from this year
    # before appending the freshly computed ones. this avoids duplicates
    # when we re-run the update for the same year multiple times.
    if os.path.exists(out_path):
        existing = pd.read_parquet(out_path)
        year_prefix = str(year)
        # 'date' is stored as ISO strings like "2024-04-08", so str.startswith("2024")
        # is the simplest way to drop all rows for a given year without parsing dates.
        keep_mask = ~existing['date'].astype(str).str.startswith(year_prefix)
        existing = existing[keep_mask]
        combined = pd.concat([existing, new_df], ignore_index=True)
    else:
        combined = new_df

    combined.to_parquet(out_path, index=False)
    print(f'corpus now has {len(combined)} total rows -> {out_path}')


def _extract_patterns_from_events(events, year):
    '''scan a dataframe of retrosheet events for one year and return a list of
    pattern-occurrence dicts, one per detected instance.

    each returned dict has:
      date        — YYYY-MM-DD string extracted from GAME_ID
      team        — 3-letter retrosheet team code for the team that did the thing
      pattern     — snake_case string name of the pattern (see below)
      description — human-readable sentence describing what happened
      game_id     — the full GAME_ID string for cross-referencing

    we filter to BAT_EVENT_FL == 'T' right away to drop baserunning micro-events
    that share a plate appearance with the actual at-bat result. keeping them
    would double-count things like "batter hit HR while a runner was caught stealing."
    '''
    # guard against missing columns — older retrosheet files sometimes lack
    # columns that were added to the spec later (e.g. PITCH_SEQ_TX pre-1950)
    required_cols = {'GAME_ID', 'BAT_EVENT_FL', 'INN_CT', 'BAT_HOME_ID',
                     'EVENT_CD', 'BAT_ID', 'HOME_TEAM_ID', 'AWAY_TEAM_ID',
                     'HOME_SCORE_CT', 'AWAY_SCORE_CT'}
    missing = required_cols - set(events.columns)
    if missing:
        print(f'  year {year}: missing columns {missing}, skipping')
        return []

    # keep only true batting events — drops baserunning rows that share a PA
    batting = events[events['BAT_EVENT_FL'] == 'T'].copy()

    # derive the batting team name from the home/away flag so downstream
    # code doesn't have to think about it
    batting['batting_team'] = batting.apply(_batting_team, axis=1)

    # extract the date from GAME_ID — format is like "ANA200304080"
    # characters at index 3-10 are YYYYMMDD. we convert to ISO string.
    batting['date'] = batting['GAME_ID'].apply(_game_id_to_date)

    rows = []
    for game_id, game_df in batting.groupby('GAME_ID'):
        rows.extend(_detect_back_to_back_hr_inning(game_df, game_id))
        rows.extend(_detect_three_hr_inning(game_df, game_id))
        rows.extend(_detect_walk_off_grand_slam(game_df, game_id))
        rows.extend(_detect_immaculate_inning(game_df, game_id))
        rows.extend(_detect_position_player_pitching(game_df, game_id))

    return rows


# ---- pattern detectors ------------------------------------------------------
# each detector takes a dataframe scoped to one game and the game_id string,
# and returns a list of dicts (empty list if the pattern didn't occur).

def _detect_back_to_back_hr_inning(game_df, game_id):
    '''back_to_back_hr_inning: two consecutive batters both hit HRs in the same
    inning for the same team.

    "consecutive" means no other plate appearances intervened — it's not
    back-to-back if there was a walk in between.
    '''
    results = []

    for (inn, bat_home), half_inning in game_df.groupby(['INN_CT', 'BAT_HOME_ID']):
        half_inning = half_inning.reset_index(drop=True)

        for idx in range(len(half_inning) - 1):
            row_a = half_inning.iloc[idx]
            row_b = half_inning.iloc[idx + 1]

            if int(row_a['EVENT_CD']) == EVENT_HR and int(row_b['EVENT_CD']) == EVENT_HR:
                team = row_a['batting_team']
                date = row_a['date']
                batter_a = row_a['BAT_ID']
                batter_b = row_b['BAT_ID']

                results.append({
                    'date': date,
                    'team': team,
                    'pattern': 'back_to_back_hr_inning',
                    'description': (f'{team}: {batter_a} and {batter_b} hit back-to-back HRs '
                                    f'in inning {int(inn)} ({game_id})'),
                    'game_id': game_id,
                })

    return results


def _detect_three_hr_inning(game_df, game_id):
    '''three_hr_inning: a team hits 3 or more home runs in the same half-inning.

    this is distinct from back_to_back in that the HRs don't need to be
    consecutive — there could be other plate appearances (walks, singles, outs)
    interspersed. "3+ HRs by one team in one inning" is the only requirement.
    '''
    results = []

    for (inn, bat_home), half_inning in game_df.groupby(['INN_CT', 'BAT_HOME_ID']):
        hr_rows = half_inning[half_inning['EVENT_CD'].astype(int) == EVENT_HR]

        if len(hr_rows) >= 3:
            team = half_inning.iloc[0]['batting_team']
            date = half_inning.iloc[0]['date']
            batters = ', '.join(hr_rows['BAT_ID'].tolist())

            results.append({
                'date': date,
                'team': team,
                'pattern': 'three_hr_inning',
                'description': (f'{team}: {len(hr_rows)} HRs in inning {int(inn)} '
                                f'({batters}) — {game_id}'),
                'game_id': game_id,
            })

    return results


def _detect_walk_off_grand_slam(game_df, game_id):
    '''walk_off_grand_slam: a home run with the bases loaded in the final half-inning
    that wins the game for the home team when they were trailing by 1-4 runs.

    we use BASE1_RUN_ID, BASE2_RUN_ID, BASE3_RUN_ID to detect bases loaded.
    HOME_SCORE_CT and AWAY_SCORE_CT are cumulative scores after each play.
    '''
    results = []

    base_cols = {'BASE1_RUN_ID', 'BASE2_RUN_ID', 'BASE3_RUN_ID'}
    if not base_cols.issubset(set(game_df.columns)):
        return results

    home_bats = game_df[game_df['BAT_HOME_ID'].astype(int) == 1]
    if home_bats.empty:
        return results

    final_inning = home_bats['INN_CT'].astype(int).max()
    final_half = home_bats[home_bats['INN_CT'].astype(int) == final_inning]

    for _, row in final_half.iterrows():
        if int(row['EVENT_CD']) != EVENT_HR:
            continue

        b1 = str(row.get('BASE1_RUN_ID', '')).strip()
        b2 = str(row.get('BASE2_RUN_ID', '')).strip()
        b3 = str(row.get('BASE3_RUN_ID', '')).strip()
        if not (b1 and b2 and b3):
            continue

        home_after = int(row['HOME_SCORE_CT'])
        away_after = int(row['AWAY_SCORE_CT'])
        # grand slams always score exactly 4 runs (batter + 3 runners on base),
        # so subtracting 4 is always correct for a true grand slam.
        home_before = home_after - 4
        deficit = away_after - home_before

        if home_after > away_after and 1 <= deficit <= 4:
            team = row['batting_team']
            date = row['date']
            batter = row['BAT_ID']

            results.append({
                'date': date,
                'team': team,
                'pattern': 'walk_off_grand_slam',
                'description': (f'{batter} ({team}) walk-off grand slam in the bottom of inning '
                                f'{final_inning}, trailing {deficit} before the swing — {game_id}'),
                'game_id': game_id,
            })

    return results


def _detect_immaculate_inning(game_df, game_id):
    '''immaculate_inning: a pitcher records all 3 outs via strikeout using exactly
    9 pitches in a single half-inning. every pitch must be a strike.

    we detect it by reading PITCH_SEQ_TX for each at-bat:
      S = swinging strike, C = called strike, K = strikeout-last-pitch,
      T = foul tip completing a strikeout (also a valid called strike)
      non-strike pitch chars (B=ball, F=foul, etc.) disqualify the sequence.

    two guards beyond the pitch-sequence check:
      1. the half-inning must have exactly 3 plate appearances total — no walks,
         hits, errors, etc. "3 strikeouts in an inning that also had a walk"
         is NOT an immaculate inning.
      2. each of those 3 at-bats must consist entirely of strike pitches (all 3
         pitches per PA must be strikes, 9 total across the half-inning).
    '''
    results = []

    if 'PITCH_SEQ_TX' not in game_df.columns:
        return results

    for (inn, bat_home), half_inning in game_df.groupby(['INN_CT', 'BAT_HOME_ID']):
        # an immaculate inning has exactly 3 batters — no walks, hits, or other
        # batters who reached base and then had the inning continue.
        if len(half_inning) != 3:
            continue

        strikeout_rows = half_inning[half_inning['EVENT_CD'].astype(int) == EVENT_STRIKEOUT]

        if len(strikeout_rows) != 3:
            continue

        all_9_pitches = True
        for _, row in strikeout_rows.iterrows():
            seq = str(row.get('PITCH_SEQ_TX', '')).strip()
            if not seq:
                all_9_pitches = False
                break

            # pitch sequence alphabet (Retrosheet spec):
            #   S = swinging strike,  C = called strike,  K = third strike (swinging)
            #   T = foul tip completing the strikeout — counts as a strike
            #   B = ball,  F = foul ball,  H = hit-by-pitch — any of these disqualify
            #   > + * 1 2 3 N V = non-pitch annotations (runner events, pitchout
            #     context flags, etc.) — we ignore these when validating pitch count
            pitches_only = [ch for ch in seq if ch in 'SCKT']
            non_strike = [ch for ch in seq if ch not in 'SCKT>+*123NV']

            if non_strike:
                all_9_pitches = False
                break

            if len(pitches_only) != 3:
                all_9_pitches = False
                break

        if all_9_pitches:
            pitcher = strikeout_rows.iloc[0]['PIT_ID']
            home_id = strikeout_rows.iloc[0]['HOME_TEAM_ID']
            away_id = strikeout_rows.iloc[0]['AWAY_TEAM_ID']
            pitching_team = home_id if int(bat_home) == 0 else away_id
            date = strikeout_rows.iloc[0]['date']
            # pull the half-inning word out so the f-string stays single-quoted
            half = 'bottom' if int(bat_home) == 1 else 'top'

            results.append({
                'date': date,
                'team': pitching_team,
                'pattern': 'immaculate_inning',
                'description': (
                    f'{pitcher} ({pitching_team}): immaculate inning in the '
                    f'{half} of inning {int(inn)} — 9 pitches, 3 Ks — {game_id}'
                ),
                'game_id': game_id,
            })

    return results


def _detect_position_player_pitching(game_df, game_id):
    '''position_player_pitching: a non-pitcher appears as pitcher in a game.

    heuristic: find players who batted before they pitched (chronologically),
    excluding game starting pitchers.

    why the naive set-intersection approach fails:
      the old approach was: (all_batters & all_pitchers) - starting_pitchers.
      this produces massive false positives in NL games (pre-2022 universal DH)
      because any NL relief pitcher who also batted in the same game appears in
      both BAT_ID (when their team bats) and PIT_ID (when the opposing team bats).
      relief pitchers are NOT starting pitchers so PIT_START_FL is 'F' for them —
      they are not excluded — and they flood the corpus with false hits. a typical
      NL game with 3-4 pitchers per team would generate 4-8 false positives per
      game, across ~100 years of NL play.

    the fix — row-ordering heuristic:
      a real pitcher's FIRST appearance in PIT_ID happens before they bat (they
      pitch first, then come to the plate in their team's half-inning). a position
      player pressed into service as pitcher has already batted multiple times
      before they appear in PIT_ID — they pitched last, as a blowout measure.

      concretely: if ANY bat-event row for this player precedes their first
      pitch-event row, they are a position player pitcher.

      edge case this misses: a pitcher used as a pinch-hitter in inning N and
      then brought in to pitch in inning N+1. that is extremely rare and the
      false positive cost (one extra corpus row) is acceptable.
    '''
    results = []

    # note: game_df has already been filtered to BAT_EVENT_FL == 'T' by the
    # caller (_extract_patterns_from_events), so every row here is a true
    # batting event — baserunning micro-events are not present.

    if 'PIT_ID' not in game_df.columns:
        return results

    # we need row ordering to be reliable for chronological comparison.
    # game_df should already be in event order (cwevent outputs sequentially),
    # but reset the index so iloc positions are contiguous.
    gdf = game_df.reset_index(drop=True)

    # collect starting pitchers so we can exclude them entirely.
    starting_pitchers = set()
    if 'PIT_START_FL' in gdf.columns:
        starting_pitchers = set(gdf[gdf['PIT_START_FL'] == 'T']['PIT_ID'].unique())

    # find all non-starter pitchers who appeared in this game.
    all_pitchers = set(gdf['PIT_ID'].unique())
    relief_and_others = all_pitchers - starting_pitchers

    # among those, find any who ALSO batted.
    all_batters = set(gdf['BAT_ID'].unique())
    candidates = relief_and_others & all_batters

    if not candidates:
        return results

    for player_id in candidates:
        # find the first row where this player PITCHED (appeared in PIT_ID)
        pitcher_rows = gdf[gdf['PIT_ID'] == player_id]
        if pitcher_rows.empty:
            continue
        first_pitch_idx = pitcher_rows.index[0]

        # find any rows where this player BATTED (appeared in BAT_ID)
        batter_rows = gdf[gdf['BAT_ID'] == player_id]
        if batter_rows.empty:
            continue

        # key filter: at least one batting appearance must come BEFORE
        # their first pitching appearance. a real pitcher's batting rows
        # (in NL games) are scattered throughout the game interspersed with
        # their pitching; a position player pressed into service as pitcher
        # will always have batted first and only pitched late in the game.
        # this is not airtight for early-game position-player appearances,
        # but it eliminates the vast majority of NL-pitcher false positives.
        batted_before_pitching = (batter_rows.index < first_pitch_idx).any()
        if not batted_before_pitching:
            continue

        # belt-and-suspenders: also require at least one pitching row came AFTER
        # the player's last at-bat. this catches the (very rare) scenario where
        # a player batted early in the game, then had no more PAs, and the only
        # pitching appearance was in between two of their batting appearances —
        # which would be bizarre and is not what we mean by position-player pitching.
        last_bat_idx = batter_rows.index[-1]
        pitched_after_last_at_bat = (pitcher_rows.index > last_bat_idx).any()
        if not pitched_after_last_at_bat:
            continue

        first_pitch = pitcher_rows.iloc[0]
        date = first_pitch['date']
        bat_home_when_pitching = int(first_pitch['BAT_HOME_ID'])
        home_id = first_pitch['HOME_TEAM_ID']
        away_id = first_pitch['AWAY_TEAM_ID']
        # when the batter's team (bat_home_when_pitching) is at bat, the OTHER
        # team is pitching. so the pitching_team is the OPPOSITE side.
        pitching_team = home_id if bat_home_when_pitching == 0 else away_id

        results.append({
            'date': gdf.loc[batter_rows.index[0], 'date'],
            'team': pitching_team,
            'pattern': 'position_player_pitching',
            'description': (f'{player_id} ({pitching_team}) pitched as a position player '
                            f'in {game_id}'),
            'game_id': game_id,
        })

    return results


# ---- helper utilities -------------------------------------------------------

def _batting_team(row):
    '''return the 3-letter team code of the team that is currently batting.
    BAT_HOME_ID == 1 means the home team is batting, == 0 means visitors.
    '''
    if int(row['BAT_HOME_ID']) == 1:
        return row['HOME_TEAM_ID']
    return row['AWAY_TEAM_ID']


def _game_id_to_date(game_id):
    '''extract an ISO date string from a retrosheet GAME_ID.
    game_id format: HOMETEAMyyyymmddN, e.g. "ANA200304080" -> "2003-04-08"
    '''
    try:
        date_str = game_id[3:11]
        return f'{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}'
    except (IndexError, TypeError):
        return game_id


# ---- main entry point -------------------------------------------------------

if __name__ == '__main__':
    import sys

    out_path = 'retrosheet-patterns.parquet'

    if len(sys.argv) == 3:
        start = int(sys.argv[1])
        end = int(sys.argv[2])
        print(f'building corpus for {start}-{end}')
        build_pattern_corpus(start_year=start, end_year=end, out_path=out_path)
    else:
        print('building full corpus 1918-2024 (this will take a while...)')
        build_pattern_corpus(out_path=out_path)

    print(f'corpus written to {out_path} — commit it to the repo to make it available to the daily workflow')
