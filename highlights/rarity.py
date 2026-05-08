'''
MLB highlights rarity engine.

deterministically identifies rare/historic events from box scores and
play-by-play data. we check actual facts here, using deterministic logis
so the downstream AI call doesn't have to make them up.
asking the AI to actually analyze todays scores against a the archive
gets a lotta hallucinations as the context window gets too big for current models.

three tiers of rarity, each with a different data source:

  tier 1 — single-game rarities: done right from the box score or
            play-by-play payload that get_scores.py already pulled.

  tier 2 — season ledger: a small json file in repo data/ that we update.
            tracks things like "has this team thrown a no-hitter this
            season?" or "how long is this player's current hit streak?"
            easy to load and read because it's small, 
            and we update it in-place for chnges
            the workflow commits it back to the repo after each run.

  tier 3 — historical corpus: retrosheet-patterns.parquet in the repo root,
            built from retrosheet event data going back decades. we match
            today's patterns against it to surface "first time in X years"
            facts. wrapped in try/except so a missing parquet file degrades
            gracefully instead of taking down tier 1/2 results
            
---- Why parquet?
Originally we were looking at either CSV or SQL for retrosheet, both had pretty severe downsides.
Both Gemini and Claude suggested parquet as a lightweight and fast columnuar option to lookup 
this allows fast looks on event pattens + team

final parquet will have lines like:
         date           team     pattern                    description                game_id
193   │  "1923-07-24"  "CIN"     "walk_off_grand_slam"      "hargb101 (CIN) walk-off…  "CIN192307240"                                                                       
194   │  "1923-05-04"  "SLA"     "back_to_back_hr_inning"   "SLA: willk101 and dursc…  "CLE192305040"                                                                       
195   │  "1923-07-13"  "PHA"     "position_player_pitchin…  "ogdec101 (PHA) pitched …  "CLE192307130"                                                                       
196   │  "1923-06-26"  "DET"     "back_to_back_hr_inning"   "DET: manuh101 and heilh…  "DET192306260"                                                                       
197   │  "1923-07-12"  "BOS"     "back_to_back_hr_inning"   "BOS: burng102 and harrj…  "DET192307120"                                                                       
198   │  "1923-04-28"  "BSN"     "back_to_back_hr_inning"   "BSN: mcins101 and bagwb…  "NY1192304280"                                                                       
199   │  "1923-05-30"  "NY1"     "walk_off_grand_slam"      "grohh101 (NY1) walk-off…  "NY1192305302"                                                                       
200   │  "1923-07-10"  "NY1"     "back_to_back_hr_inning"   "NY1: meusi101 and cunnb…  "NY1192307100"   
--------------------
'''

import datetime
import json
import os

import pandas


def find_rare_events(box_scores, target_date):
    '''entry point. returns a flat list of verified rare event dicts.

    each dict has at minimum:
      kind        — machine-readable label, e.g. "no_hitter"
      label       — human-readable short label, e.g. "No-hitter"
      description — full sentence ready for the AI narrator to quote or riff on
      since       — ISO date string of last occurrence, or a plain-english
                    phrase like "first time in available records"

    callers (main.py) iterate this list without caring which tier it came
    from — everything surfaces through this single return value.
    '''
    events = []  # create empty events arary to add to

    # tier 1: single-game, box-score checks
    # these are fast, and fully deterministic
    # get_scores.py already got everything we need for this
    for box in box_scores:
        # for each line of the box score, run through the checks later in here
        # if anything runs back, add to the events
        events.extend(_check_cycle(box))
        events.extend(_check_no_hitter(box))
        events.extend(_check_immaculate_inning(box))
        events.extend(_check_multi_hr_individual(box, threshold=3))
        events.extend(_check_position_player_pitching(box))
        events.extend(_check_double_digit_k(box))
        events.extend(_check_shutout(box))

    # tier 2: season ledger from json
    # a read/write failure should just go missing, 
    # wont break the tier 1 results already processed above
    try:
        ledger_events, new_ledger = _check_season_ledger(box_scores, target_date, tier1_events=events)
        # use the tier 1 events already processed to the _check_season_ledger function
        events.extend(ledger_events)  # add anything returned for season ledger to the 
        if new_ledger is not None:
            _write_season_ledger(new_ledger)
    except Exception as exception:
        print(f'tier 2 season ledger unavailable, skipping: {exception}')

    # tier 3: historical corpus pattern matching.
    # this can fail gracefully — if the parquet is missing (first run, tests)
    # we just skip it rather than blowing up the whole pipeline.
    try:
        corpus = _load_pattern_corpus()  # get pandas to read and return the corpus
        patterns = _extract_patterns(box_scores)
        for pattern in patterns:
            # find all prior occurrences of this exact pattern for this team
            prior = corpus[
                (corpus.team == pattern['team']) &
                (corpus.pattern == pattern['pattern']) &
                (corpus.date < target_date)
            ]
            if not prior.empty:
                last_seen = prior.date.max()
                gap = (target_date - last_seen).days
                # 5 years == ~1825 days — anything shorter isn't really rare.
                # we don't want to surface "first time since last tuesday."
                if gap > 365 * 5:
                    events.append({
                        'kind': pattern['pattern'],
                        'label': pattern['label'],
                        'description': pattern['description'],
                        'since': last_seen.isoformat(),
                    })
    except Exception as exception:
        # broad catch intentional — tier 3 is good but way more complex
        # we still want the tier 1/2 events to make it through if this fails
        print(f'warning: skipping tier 3 corpus check — {exception}')

    print(f'rarity engine: {len(events)} event(s) found')
    return events


# tier 1 events helpers

def _check_cycle(box):
    '''look for a player who hit for the cycle (1B, 2B, 3B, HR in one game).

    the cycle is rare because triples are rare
    so getting one in the same game as a HR and 2B is gonna be unlikely

    how we detect it from the box score:
    * batting stats give us: hits, doubles, triples, homeRuns
    * singles = hits - doubles - triples - homeRuns
    * a cycle requires singles >= 1 AND doubles >= 1 AND triples >= 1
    * AND homeRuns >= 1
    '''
    events = []

    # statsapi.boxscore_data() puts home/away at the top level of the dict,
    # not nested under a 'teams' key — so we access box['home'] / box['away'].
    # box[side]['team'] only contains {'id'} — the full name is in teamInfo.
    for side in ('away', 'home'):
        side_data = box.get(side, {})
        batters = side_data.get('batters', [])
        batter_stats = side_data.get('players', {})
        team_name = box.get('teamInfo', {}).get(side, {}).get('teamName', 'Unknown')

        for pid in batters:  # get each player ID in the batters arrray 
            player_key = f'ID{pid}'
            stats = (
                batter_stats.get(player_key, {})
                            .get('stats', {})
                            .get('batting', {})
            )  # get the batting stats for this player for this game
            if not stats:
                continue

            hits    = stats.get('hits', 0)
            doubles = stats.get('doubles', 0)
            triples = stats.get('triples', 0)
            hrs     = stats.get('homeRuns', 0)

            # need at least 4 hits to have all four hit types, otherwise we can bail now
            if hits < 4:
                continue

            # singles aren't a direct field , we back calculatae out from total hits
            # this shouldn't ever go negative but we
            # check with >= 1 which handles that in case
            singles = hits - doubles - triples - hrs

            if singles >= 1 and doubles >= 1 and triples >= 1 and hrs >= 1:
                name = (
                    batter_stats.get(player_key, {})
                                .get('person', {})
                                .get('fullName', f'Player {pid}')
                )
                print(f'cycle detected: {name} ({team_name})')
                events.append({
                    'kind': 'cycle',
                    'label': 'Hit for the Cycle',
                    # build a sentence the AI narrator can quote directly
                    'description': (
                        f'{name} ({team_name}) hit for the cycle: '
                        f'{singles} single(s), {doubles} double(s), '
                        f'{triples} triple(s), {hrs} home run(s).'
                    ),
                    # tier 1 events don't know the historical "since" —
                    # that would come from tier 3 corpus matching
                    'since': None,
                })

    return events


def _check_no_hitter(box):
    '''detect no-hitters (and perfect games since thats also a no hitter).

    we look at each pitchers final line:
      * inningsPitched == 9.0 (or more, for extras)
      * hits == 0 — no hit allowed
      * we also check walks and HBP to distinguish a perfect game from a
         no-hitter, since perfect games are just way way way more rarer

    note: combined no-hitters aren't picked up here because
    the individual lines will each show < 9 IP. that would
    need aggregation across pitchers and its more than i wanna handle
    '''
    events = []

    # statsapi.boxscore_data() puts home/away at the top level, not under 'teams'.
    # box[side]['team'] only contains {'id'} — the full name is in teamInfo.
    for side in ('away', 'home'):
        side_data = box.get(side, {})
        pitchers = side_data.get('pitchers', [])
        player_data = side_data.get('players', {})
        team_name = box.get('teamInfo', {}).get(side, {}).get('teamName', 'Unknown')

        for pid in pitchers:  # get each player ID in the pitchers array 
            player_key = f'ID{pid}'
            stats = (
                player_data.get(player_key, {})
                           .get('stats', {})
                           .get('pitching', {})
            )
            if not stats:
                continue

            innings_raw = stats.get('inningsPitched', '0.0')
            hits = stats.get('hits', 0)

            '''inningsPitched is a string like "9.0" or "9.1" where the digit
            after the decimal is *outs* (0, 1, or 2), not tenths of an inning.
            we gotta use the same conversion from before
            ----
            using _parse_innings_pitched from statlines is cleaner but would
            create a cross-module dependency so just exact the same logic here'''
            try:
                parts = str(innings_raw).split('.')
                full_inn = int(parts[0])
                outs = int(parts[1]) if len(parts) > 1 else 0
                innings_float = full_inn + outs / 3.0
            except (ValueError, IndexError):
                continue

            # need at least a complete 9-inning game (27 outs) to qualify
            if innings_float >= 9.0 and hits == 0:
                name = (
                    player_data.get(player_key, {})
                               .get('person', {})
                               .get('fullName', f'Player {pid}')
                )
                # perfect game = no hits, no walks, no HBP
                walks = stats.get('baseOnBalls', 0)
                hbp   = stats.get('hitBatsmen', 0)
                is_perfect = (walks == 0 and hbp == 0)

                # label if it was perfect or a no-no
                kind  = 'perfect_game' if is_perfect else 'no_hitter'
                label = 'Perfect Game' if is_perfect else 'No-Hitter'

                ks = stats.get('strikeOuts', 0)
                print(f'{label} detected: {name} ({team_name}), {ks} K')
                events.append({
                    'kind': kind,
                    'label': label,
                    'description': (
                        f'{name} ({team_name}) threw a {label.lower()} — '
                        f'{ks} strikeout(s), '
                        f'{innings_raw} inning(s) pitched.'
                    ),
                    'since': None,
                })

    return events


def _ordinal(num):
    '''return the ordinal string for an integer, e.g. 1 -> "1st", 3 -> "3rd".'''
    num = int(num)
    suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(num % 10, 'th')
    if 11 <= num % 100 <= 13:
        suffix = 'th'
    return f'{num}{suffix}'


def _check_immaculate_inning(box):
    '''detect an immaculate inning: 3 strikeouts on exactly 9 pitches in one half-inning.

    play-by-play data included in the box dict as 'play_by_play' from main function.
    each play is checked for event == "Strikeout" and *exactly* 3 pitches from pitchIndex length
    '''
    plays_data = box.get('play_by_play', {})
    if not plays_data:
        return []

    all_plays = plays_data.get('allPlays', [])
    plays_by_inning = plays_data.get('playsByInning', [])
    events = []

    for inning_data in plays_by_inning:
        inning_num = inning_data.get('num', '?')
        for half in ('top', 'bottom'):
            play_indices = inning_data.get(half, [])
            if len(play_indices) != 3:
                continue

            half_plays = [all_plays[pi] for pi in play_indices if pi < len(all_plays)]
            if len(half_plays) != 3:
                continue

            # all 3 must be strikeouts
            if not all(play.get('result', {}).get('event') == 'Strikeout' for play in half_plays):
                continue

            # each must have been exactly 3 pitches
            if not all(len(play.get('pitchIndex', [])) == 3 for play in half_plays):
                continue

            pitcher = half_plays[0].get('matchup', {}).get('pitcher', {})
            pitcher_name = pitcher.get('fullName', 'Unknown Pitcher')
            # top half = away team batting, so home team is pitching; bottom = vice versa
            pitching_side_key = 'home' if half == 'top' else 'away'
            team_name = box.get('teamInfo', {}).get(pitching_side_key, {}).get('teamName', 'Unknown')
            ordinal = _ordinal(inning_num)

            print(f'immaculate inning: {pitcher_name} ({team_name}), inning {inning_num}')
            events.append({
                'kind': 'immaculate_inning',
                'label': 'Immaculate Inning',
                'description': (
                    f'{pitcher_name} ({team_name}) threw an immaculate inning '
                    f'in the {ordinal} — 3 strikeouts on 9 pitches.'
                ),
                'since': None,
            })

    return events


def _check_multi_hr_individual(box, threshold=3):
    '''find any player who hit threshold or more home runs in a single game.

    3 HRs in a game is genuinely rare (~20-30 times per season across all
    of MLB). 4 HRs in a game has happened fewer than 20 times in history.
    we surface both — threshold=3 catches 3-HR games, and if someone hit 4
    the description will say so.
    '''
    events = []

    # statsapi.boxscore_data() puts home/away at the top level, not under 'teams'.
    # box[side]['team'] only contains {'id'} — the full name is in teamInfo.
    for side in ('away', 'home'):
        side_data = box.get(side, {})
        batters = side_data.get('batters', [])
        player_data = side_data.get('players', {})
        team_name = box.get('teamInfo', {}).get(side, {}).get('teamName', 'Unknown')

        for pid in batters:
            player_key = f'ID{pid}'
            stats = (
                player_data.get(player_key, {})
                           .get('stats', {})
                           .get('batting', {})
            )
            if not stats:
                continue

            hrs = stats.get('homeRuns', 0)
            if hrs >= threshold:
                name = (
                    player_data.get(player_key, {})
                               .get('person', {})
                               .get('fullName', f'Player {pid}')
                )
                rbi = stats.get('rbi', 0)
                print(f'multi-HR game: {name} ({team_name}) — {hrs} HR')
                events.append({
                    'kind': 'multi_hr',
                    'label': f'{hrs}-Homer Game',
                    'description': (
                        f'{name} ({team_name}) hit {hrs} home run(s) '
                        f'with {rbi} RBI.'
                    ),
                    'since': None,
                })

    return events


def _check_position_player_pitching(box):
    '''detect position players who pitched — a sign of a blowout that's
    become its own small tradition worth calling out.

    how to tell: a player appears in both the pitchers list AND their
    primary position (from the roster) is not "P" (pitcher).

    the box score includes each player's position via:
      player_data[player_key]['position']['abbreviation']

    if that abbreviation isn't "P" but the player appears in the pitchers
    list, they're a position player who took the mound.

    we exclude two-way players (like shohei ohtani when he still pitched)
    by also checking allPositions — if any listed position is "P", they're
    a legit pitcher.
    '''
    events = []

    # statsapi.boxscore_data() puts home/away at the top level, not under 'teams'.
    # box[side]['team'] only contains {'id'} — the full name is in teamInfo.
    for side in ('away', 'home'):
        side_data = box.get(side, {})
        pitchers = side_data.get('pitchers', [])
        player_data = side_data.get('players', {})
        team_name = box.get('teamInfo', {}).get(side, {}).get('teamName', 'Unknown')

        for pid in pitchers:
            player_key = f'ID{pid}'
            player = player_data.get(player_key, {})

            primary_pos = player.get('position', {}).get('abbreviation', '')
            # allPositions covers guys listed at multiple spots (DH/P, etc.)
            all_pos = [
                pos.get('abbreviation', '')
                for pos in player.get('allPositions', [])
            ]

            # skip if they're a real pitcher at any listed position
            if 'P' in all_pos or primary_pos == 'P':
                continue

            name = player.get('person', {}).get('fullName', f'Player {pid}')
            stats = player.get('stats', {}).get('pitching', {})
            innings = stats.get('inningsPitched', '?')

            print(f'position player pitching: {name} ({primary_pos}, {team_name})')
            events.append({
                'kind': 'position_player_pitching',
                'label': 'Position Player Pitching',
                'description': (
                    f'{name} ({team_name}), normally a {primary_pos}, '
                    f'took the mound and pitched {innings} inning(s).'
                ),
                'since': None,
            })

    return events


def _check_double_digit_k(box):
    '''find any pitcher who struck out 10 or more batters in the game.'''
    events = []

    for side in ('away', 'home'):
        side_data = box.get(side, {})
        pitchers = side_data.get('pitchers', [])
        player_data = side_data.get('players', {})
        team_name = box.get('teamInfo', {}).get(side, {}).get('teamName', 'Unknown')

        for pid in pitchers:
            player_key = f'ID{pid}'
            stats = (
                player_data.get(player_key, {})
                           .get('stats', {})
                           .get('pitching', {})
            )
            if not stats:
                continue

            ks = stats.get('strikeOuts', 0)
            if ks >= 10:
                name = (
                    player_data.get(player_key, {})
                               .get('person', {})
                               .get('fullName', f'Player {pid}')
                )
                ip = stats.get('inningsPitched', '?')
                print(f'double-digit K: {name} ({team_name}) — {ks} K')
                events.append({
                    'kind': 'double_digit_k',
                    'label': '10+ Strikeout Game',
                    'description': (
                        f'{name} ({team_name}) struck out {ks} batters in {ip} IP.'
                    ),
                    'since': None,
                })

    return events


def _check_shutout(box):
    '''detect when a team's pitching staff held the opponent to zero runs.'''
    events = []

    home_data = box.get('home', {})
    away_data = box.get('away', {})
    # box[side]['team'] only contains {'id'} — the full name is in teamInfo.
    team_info = box.get('teamInfo', {})
    home_name = team_info.get('home', {}).get('teamName', 'Unknown')
    away_name = team_info.get('away', {}).get('teamName', 'Unknown')

    home_pitching = home_data.get('teamStats', {}).get('pitching', {})
    away_pitching = away_data.get('teamStats', {}).get('pitching', {})
    home_batting = home_data.get('teamStats', {}).get('batting', {})
    away_batting = away_data.get('teamStats', {}).get('batting', {})

    # home pitching staff shut out the away team — display home team's run total
    if home_pitching.get('runs', 1) == 0:
        score = home_batting.get('runs', 0)
        print(f'shutout: {away_name} shut out by {home_name} ({score}-0)')
        events.append({
            'kind': 'shutout',
            'label': 'Shutout',
            'description': (
                f'{home_name} shut out {away_name} ({score}-0).'
            ),
            'since': None,
        })

    # away pitching staff shut out the home team — display away team's run total
    if away_pitching.get('runs', 1) == 0:
        score = away_batting.get('runs', 0)
        print(f'shutout: {home_name} shut out by {away_name} ({score}-0)')
        events.append({
            'kind': 'shutout',
            'label': 'Shutout',
            'description': (
                f'{away_name} shut out {home_name} ({score}-0).'
            ),
            'since': None,
        })

    return events


# ---- tier 2: season ledger --------------------------------------------------

def _check_season_ledger(box_scores, target_date, tier1_events=None):
    '''check/update the season ledger: a small JSON file in data/ that tracks
    ongoing per-season streaks and "season firsts."

    returns (events, new_ledger) — new_ledger is always non-None; the
    caller writes it back to disk unconditionally (cheap enough at once/day).
    '''
    ledger = _load_season_ledger()
    streaks = ledger.get('hitting_streaks', {})
    no_hitter_teams = ledger.get('no_hitter_teams', [])
    season_firsts = ledger.get('season_firsts', {})

    events = []
    date_str = target_date.isoformat()

    # hitting streaks: update every batter from every box score
    _STREAK_MILESTONES = {20, 30, 40}
    for box in box_scores:
        for side in ('away', 'home'):
            side_data = box.get(side, {})
            batters = side_data.get('batters', [])
            player_data = side_data.get('players', {})
            team_name = box.get('teamInfo', {}).get(side, {}).get('teamName', 'Unknown')

            for pid in batters:
                player_key = f'ID{pid}'
                batting = (
                    player_data.get(player_key, {})
                               .get('stats', {})
                               .get('batting', {})
                )
                if not batting:
                    continue

                hits = batting.get('hits', 0)
                # plate appearances = at bats + walks + sac flies etc.; skip
                # guys who didn't actually bat (e.g. defensive subs)
                ab = batting.get('atBats', 0)
                if ab == 0:
                    continue

                pid_str = str(pid)
                prev = streaks.get(pid_str, {'streak': 0})
                prev_streak = prev.get('streak', 0)

                if hits > 0:
                    new_streak = prev_streak + 1
                else:
                    new_streak = 0

                name = (
                    player_data.get(player_key, {})
                               .get('person', {})
                               .get('fullName', f'Player {pid}')
                )
                streaks[pid_str] = {
                    'name': name,
                    'team': team_name,
                    'streak': new_streak,
                    'last_date': date_str,
                }

                # emit once per milestone crossing (prev was just below, now at/above)
                for milestone in _STREAK_MILESTONES:
                    if prev_streak < milestone <= new_streak:
                        print(f'hit streak milestone: {name} at {new_streak} games')
                        events.append({
                            'kind': 'hit_streak',
                            'label': f'{new_streak}-Game Hit Streak',
                            'description': (
                                f'{name} ({team_name}) has hit safely in '
                                f'{new_streak} consecutive games.'
                            ),
                            'since': None,
                        })

    # season firsts: cycle
    tier1_events = tier1_events or []
    tier1_kinds = [event.get('kind') for event in tier1_events]

    if 'cycle' in tier1_kinds and 'cycle' not in season_firsts:
        season_firsts['cycle'] = date_str
        # find who did it from the tier 1 event for the description
        cycle_event = next(event for event in tier1_events if event.get('kind') == 'cycle')
        events.append({
            'kind': 'season_first_cycle',
            'label': "Season's First Cycle",
            'description': (
                f'First hit-for-the-cycle of the season: {cycle_event["description"]}'
            ),
            'since': None,
        })

    # season firsts / notable: no-hitter teams
    for event in tier1_events:
        if event.get('kind') not in ('no_hitter', 'perfect_game'):
            continue
        # extract team name from the description — it's in parentheses after the pitcher name
        # e.g. "John Doe (Boston Red Sox) threw a no-hitter..."
        desc = event.get('description', '')
        team_name_in_desc = ''
        if '(' in desc and ')' in desc:
            team_name_in_desc = desc[desc.index('(') + 1 : desc.index(')')]

        if not team_name_in_desc:
            continue

        if team_name_in_desc not in no_hitter_teams:
            no_hitter_teams.append(team_name_in_desc)
            # only call it the "first of the season" if no no-hitter has happened yet
            is_first_of_season = 'no_hitter' not in season_firsts
            if is_first_of_season:
                season_firsts['no_hitter'] = date_str
                events.append({
                    'kind': 'season_first_no_hitter',
                    'label': "Season's First No-Hitter",
                    'description': f'First no-hitter of the season: {desc}',
                    'since': None,
                })

    new_ledger = {
        'hitting_streaks': streaks,
        'no_hitter_teams': no_hitter_teams,
        'season_firsts': season_firsts,
    }
    return (events, new_ledger)


def _load_season_ledger():
    '''read the season ledger JSON from a local file.

    returns an empty dict if the file doesn't exist yet (start of season).

    local path: data/season-ledger-{year}.json (relative to repo root)
    using the year in the filename means it auto-resets each season.
    '''
    year = datetime.date.today().year
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'data',
        f'season-ledger-{year}.json',
    )
    if not os.path.exists(path):
        return {}
    with open(path, 'r') as fh:
        return json.load(fh)


def _write_season_ledger(ledger):
    '''write the updated season ledger back to a local file.

    we overwrite the whole file each time — it's small (a few KB) and
    doing a full replace is simpler and safer than a partial update.
    concurrent writes are theoretically possible but in practice this
    function only runs once per day, so it's fine.
    '''
    year = datetime.date.today().year
    data_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'data',
    )
    # create the data/ directory if it doesn't exist (safety — it will always
    # exist in the repo, but this makes the function self-contained)
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, f'season-ledger-{year}.json')
    with open(path, 'w') as fh:
        json.dump(ledger, fh, indent=2, default=str)


# ---- tier 3: historical data in parquet

def _load_pattern_corpus():
    '''load the retrosheet-derived pattern corpus from a local parquet file.

    the parquet schema we expect:
      date    (datetime64[ns])  — game date
      team    (str)             — team abbreviation, e.g. "NYY", "BOS"
      pattern (str)             — machine-readable pattern label
      label   (str)             — human-readable pattern label
      description (str)         — plain-english sentence describing the event

    building this file is a one-time ETL job (corpus.py) that parses
    retrosheet event files and materializes one row per "notable pattern"
    detected per game.

    local path: retrosheet-patterns.parquet in the repo root
    '''
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'retrosheet-patterns.parquet',
    )
    datafile = pandas.read_parquet(path)
    # make sure date is actually a date type for comparison arithmetic
    # convert the date from the retrosheet into something for python datetime
    # e.g. "1923-07-24" -> "1923-07-24 00:00:00" -> "1923, 7, 24"
    datafile['date'] = pandas.to_datetime(datafile['date']).dt.date
    print(f'loaded pattern corpus: {len(datafile):,} rows')
    return datafile


def _extract_patterns(box_scores):
    '''get today's patterns from box scores for historical matching

    each returned item is a dict with:
      team        — team abbreviation (must match corpus)
      pattern     — machine label (must match corpus values)
      label       — human-readable label
      description — full sentence for the event dict

    patterns extracted here will match the pattern names that corpus.py
    writes into the parquet in creation — the team+pattern key is how we join.
    '''
    patterns = []  # open empty array of patterns

    for box in box_scores:
        for side in ('away', 'home'):
            side_data = box.get(side, {})
            # box['home']['team'] only has 'id' and 'name' — abbreviation lives
            # in box['teamInfo'][side] (from gameData.teams), not in the boxscore team dict
            team_abbrev = box.get('teamInfo', {}).get(side, {}).get('abbreviation', '')
            team_name = side_data.get('team', {}).get('name', team_abbrev)
            team_stats = side_data.get('teamStats', {})

            # stolen_base_burst: this team's batters stole 4+ bases
            batting_stats = team_stats.get('batting', {})
            stolen_base_burst = batting_stats.get('stolenBases', 0)
            if stolen_base_burst >= 4 and team_abbrev:
                patterns.append({
                    'team': team_abbrev,
                    'pattern': 'stolen_base_burst',
                    'label': '4+ Stolen Bases',
                    'description': (
                        f'{team_name} stole {stolen_base_burst} bases in the game.'
                    ),
                })

        # back_to_back_hr_inning and three_hr_inning: need play-by-play data
        # to find consecutive HRs in the same half-inning. check both sides at
        # once since play-by-play is structured by inning, not by batting team.
        ## TO DO
        patterns.extend(_extract_inning_hr_patterns(box))

        # walk_off_grand_slam: home team hits a bases-loaded walk-off HR.
        # needs play-by-play to know baserunner state at the moment of the HR.
        patterns.extend(_extract_walk_off_grand_slam(box))

        # immaculate_inning: 3 Ks on exactly 9 pitches in a half-inning.
        # tier 1 already fires an event for this; here we emit the pattern
        # tuple so tier 3 can attach a "first time since X" label to it.
        ## TO DO
        patterns.extend(_extract_immaculate_inning_pattern(box))

        # position_player_pitching: a non-pitcher takes the mound.
        # tier 1 already fires an event; tier 3 gives us the historical gap.
        patterns.extend(_extract_position_player_pitching_pattern(box))

    return patterns


def _extract_inning_hr_patterns(box):
    '''scan play-by-play for back-to-back HRs or 3+ HRs in one half-inning.

    back_to_back_hr_inning: two consecutive batters hit HRs (no other PA between).
    three_hr_inning: any 3+ HRs by the same team in one half-inning (not
    necessarily consecutive — walks/singles in between are fine).

    both need play-by-play data bundled into box['play_by_play'] by main.py.
    if play-by-play is absent we skip gracefully — the box score alone doesn't
    record which inning each HR happened in.
    '''
    patterns = []
    plays_data = box.get('play_by_play', {})
    if not plays_data:
        return patterns

    all_plays = plays_data.get('allPlays', [])
    plays_by_inning = plays_data.get('playsByInning', [])

    for inning_data in plays_by_inning:
        inning_num = inning_data.get('num', '?')
        for half in ('top', 'bottom'):
            play_indices = inning_data.get(half, [])
            half_plays = [all_plays[pi] for pi in play_indices if pi < len(all_plays)]
            if not half_plays:
                continue

            # batting team for this half-inning: top = away, bottom = home
            batting_side = 'away' if half == 'top' else 'home'
            team_abbrev = box.get('teamInfo', {}).get(batting_side, {}).get('abbreviation', '')
            team_name = box.get('teamInfo', {}).get(batting_side, {}).get('teamName', team_abbrev)
            if not team_abbrev:
                continue

            # collect event names in PA order so we can look for consecutive HRs
            events = [play.get('result', {}).get('event', '') for play in half_plays]
            hr_count = events.count('Home Run')

            # three_hr_inning: 3+ HRs regardless of order
            if hr_count >= 3:
                patterns.append({
                    'team': team_abbrev,
                    'pattern': 'three_hr_inning',
                    'label': f'{hr_count} HRs in One Inning',
                    'description': (
                        f'{team_name} hit {hr_count} home run(s) in the '
                        f'{_ordinal(inning_num)} inning.'
                    ),
                })

            # back_to_back_hr_inning: any two adjacent HRs in the event list
            for idx in range(len(events) - 1):
                if events[idx] == 'Home Run' and events[idx + 1] == 'Home Run':
                    batter_a = (
                        half_plays[idx].get('matchup', {}).get('batter', {}).get('fullName', '')
                    )
                    batter_b = (
                        half_plays[idx + 1].get('matchup', {}).get('batter', {}).get('fullName', '')
                    )
                    patterns.append({
                        'team': team_abbrev,
                        'pattern': 'back_to_back_hr_inning',
                        'label': 'Back-to-Back Home Runs',
                        'description': (
                            f'{batter_a} and {batter_b} ({team_name}) hit '
                            f'back-to-back home runs in the {_ordinal(inning_num)} inning.'
                        ),
                    })
                    # only emit once per half-inning even if there are two
                    # back-to-back pairs (e.g. HRs on plays 1,2,3)
                    break

    return patterns


def _extract_walk_off_grand_slam(box):
    '''detect a walk-off grand slam from play-by-play data.

    a walk-off grand slam is a bases-loaded HR in the home team's final
    half-inning that wins the game. we look for an HR in the bottom half
    of the last inning where all three base runner fields are populated.

    play-by-play gives us matchup.postOnFirst/Second/Third for the baserunner
    state at the time of each pitch. if those fields are absent we fall back
    to checking pre-play runners via 'runners' in the play dict.
    '''
    patterns = []
    plays_data = box.get('play_by_play', {})
    if not plays_data:
        return patterns

    all_plays = plays_data.get('allPlays', [])
    plays_by_inning = plays_data.get('playsByInning', [])
    if not plays_by_inning:
        return patterns

    # only the final inning's bottom half can be a walk-off
    last_inning = plays_by_inning[-1]
    bottom_indices = last_inning.get('bottom', [])
    bottom_plays = [all_plays[pi] for pi in bottom_indices if pi < len(all_plays)]

    home_abbrev = box.get('teamInfo', {}).get('home', {}).get('abbreviation', '')
    home_name = box.get('teamInfo', {}).get('home', {}).get('teamName', home_abbrev)
    if not home_abbrev:
        return patterns

    for play in bottom_plays:
        if play.get('result', {}).get('event') != 'Home Run':
            continue

        # postOnFirst/Second/Third are post-play state — all empty after a HR
        # since every runner scored. check the pre-play runners list instead.
        matchup = play.get('matchup', {})
        runners_before = [
            runner for runner in play.get('runners', [])
            if runner.get('movement', {}).get('start') is not None
            and runner.get('movement', {}).get('end') is not None
        ]
        occupied = {runner.get('movement', {}).get('start') for runner in runners_before}
        on_first  = '1B' in occupied
        on_second = '2B' in occupied
        on_third  = '3B' in occupied

        if on_first and on_second and on_third:
            batter_name = matchup.get('batter', {}).get('fullName', '')
            patterns.append({
                'team': home_abbrev,
                'pattern': 'walk_off_grand_slam',
                'label': 'Walk-Off Grand Slam',
                'description': (
                    f'{batter_name} ({home_name}) hit a walk-off grand slam '
                    f'to win the game.'
                ),
            })
            # at most one walk-off per game
            break

    return patterns


def _extract_immaculate_inning_pattern(box):
    '''emit a tier-3 pattern tuple for each immaculate inning detected.

    the immaculate inning is already detected in tier 1 (_check_immaculate_inning).
    here we emit a corpus-matchable pattern dict so tier 3 can annotate it
    with "first time since X" context. the detection logic mirrors tier 1 exactly.
    '''
    patterns = []
    plays_data = box.get('play_by_play', {})
    if not plays_data:
        return patterns

    all_plays = plays_data.get('allPlays', [])
    plays_by_inning = plays_data.get('playsByInning', [])

    for inning_data in plays_by_inning:
        inning_num = inning_data.get('num', '?')
        for half in ('top', 'bottom'):
            play_indices = inning_data.get(half, [])
            if len(play_indices) != 3:
                continue

            half_plays = [all_plays[pi] for pi in play_indices if pi < len(all_plays)]
            if len(half_plays) != 3:
                continue

            if not all(play.get('result', {}).get('event') == 'Strikeout' for play in half_plays):
                continue

            if not all(len(play.get('pitchIndex', [])) == 3 for play in half_plays):
                continue

            # top half = away bats, so home team is pitching; bottom = vice versa
            pitching_side = 'home' if half == 'top' else 'away'
            team_abbrev = box.get('teamInfo', {}).get(pitching_side, {}).get('abbreviation', '')
            team_name = box.get('teamInfo', {}).get(pitching_side, {}).get('teamName', team_abbrev)
            pitcher_name = half_plays[0].get('matchup', {}).get('pitcher', {}).get('fullName', '')
            if not team_abbrev:
                continue

            patterns.append({
                'team': team_abbrev,
                'pattern': 'immaculate_inning',
                'label': 'Immaculate Inning',
                'description': (
                    f'{pitcher_name} ({team_name}) threw an immaculate inning '
                    f'in the {_ordinal(inning_num)} — 3 strikeouts on 9 pitches.'
                ),
            })

    return patterns


def _extract_position_player_pitching_pattern(box):
    '''emit a tier-3 pattern tuple when a position player pitches.

    the event is already detected in tier 1 (_check_position_player_pitching).
    here we emit a corpus-matchable pattern dict so tier 3 can annotate it
    with "first time since X" context. the detection logic mirrors tier 1 exactly.
    '''
    patterns = []

    for side in ('away', 'home'):
        side_data = box.get(side, {})
        pitchers = side_data.get('pitchers', [])
        player_data = side_data.get('players', {})
        team_abbrev = box.get('teamInfo', {}).get(side, {}).get('abbreviation', '')
        team_name = box.get('teamInfo', {}).get(side, {}).get('teamName', team_abbrev)
        if not team_abbrev:
            continue

        for pid in pitchers:
            player_key = f'ID{pid}'
            player = player_data.get(player_key, {})
            primary_pos = player.get('position', {}).get('abbreviation', '')
            all_pos = [pos.get('abbreviation', '') for pos in player.get('allPositions', [])]

            # skip real pitchers — same guard as tier 1
            if 'P' in all_pos or primary_pos == 'P':
                continue

            name = player.get('person', {}).get('fullName', f'Player {pid}')
            patterns.append({
                'team': team_abbrev,
                'pattern': 'position_player_pitching',
                'label': 'Position Player Pitching',
                'description': (
                    f'{name} ({team_name}), normally a {primary_pos}, '
                    f'took the mound.'
                ),
            })

    return patterns
