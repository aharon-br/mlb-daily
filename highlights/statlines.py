'''
score every player batting/pitching line from a list of box scores
and hand back the top N performers for the day for the recp

the box_scores list is expected to come from statsapi.boxscore_data() calls,
one dict per game. we iterate both sides (home/away) and both stat categories.

this will supply the servce with the list of players with statlines worth highlighting for the day
'''

from highlights import boxscore

# --------------- 
#   * event weighting *
#   this can be adusted it it feels like its highlighting too much minor things
#   or missing major things
#
#  batters weighting:
#    HR  = 4  — a home run is the biggest single-batter event; it's worth
#               treating it as roughly four times a plain hit
#    hit = 1  — baseline hit value
#    RBI = 1.5 — driving in a run is slightly more impactful than reaching base
#    run = 0.5 — scoring matters but is partly a function of teammates, player will already have 1 from hit
#    SB  = 2  — stolen bases are proactive and high-impact; we will reward them highly
#
#  pitchers weighting:
#    W   = 3  — winning is still the goal, and main scoring line
#    K   = 0.5 — strikeouts are great but a pitcher can have 6 K over 4 IP and
#                still be a mess, we don't want them to be _too_ heavy
#    IP  = 1.5 — going deep into a game is genuinely valuable and rare beyond 6+ in modern time;
#                weight it higher than raw K
#    ER  = -2  — earned runs are the clearest sign things went wrong; subtract
#                but not so harshly that one bad inning erases the good, even the best will give one up
# -------------

_BATTING_WEIGHTS = {
    'homeRuns':     4.0,
    'hits':         1.0,
    'rbi':          1.5,
    'runs':         0.5,
    'stolenBases':  2.0,
}

_PITCHING_WEIGHTS = {
    'wins':           3.0,
    'strikeOuts':     0.5,
    'inningsPitched': 1.5,   # we convert the IP to float before applying weight (e.g 6.2 == 6 2/3rds)
    'earnedRuns':    -2.0,   # takes negative off of acheivements
}

# minimum score to even be considered — keeps lowgrade out of the final lists
_BATTING_THRESHOLD  = 4
_PITCHING_THRESHOLD = 5


# use the statsapi to get the details from get_scores.py

def top_performances(box_scores, n=8, game_date=None):
    '''score every player line across all games, return the top N.

    box_scores — list of dicts from statsapi.boxscore_data()
    n — how many performers to return, we'll go with 8 as a reasonable highlight package
    game_date — ISO date string (e.g. '2026-04-29'); passed from main.py
    '''
    candidates = []  # create an empty array to store candiates for highlights

    for box in box_scores:
        date_str = game_date or box.get('gameDate', box.get('officialDate', 'unknown date'))

        for side in ('home', 'away'):
            # team names live in teamInfo, not in side['team'] (which only has {'id': ...})
            team_name = boxscore.team_name(box, side, default='unknown team')

            # figure out the opponent — whoever is on the other side of the box
            opponent = boxscore.team_name(
                box, boxscore.opponent_side(side), default='unknown opponent'
            )

            players = box.get(side, {}).get('players', {})

            # get the players and data outta the item and start a loop of them
            for player_key, player_data in players.items():
                stats = player_data.get('stats', {})

                # try batting first; default to None so the pitching block can
                # reference it safely even if there are no batting stats at all
                batting_candidate = None
                batting = stats.get('batting', {})
                if batting:
                    batting_candidate = _score_batting(
                        player_data, batting, team_name, opponent, date_str
                    )
                    if batting_candidate:
                        candidates.append(batting_candidate)

                # try pitching — two-way players (e.g. ohtani) appear in both
                # this is, frankly, really annoying and might as well call this the ohtani check
                # batting and pitching stats. to avoid listing the same player
                # twice in the top-N, we keep only the higher-scoring role
                pitching = stats.get('pitching', {})
                if pitching:
                    pitching_candidate = _score_pitching(
                        player_data, pitching, team_name, opponent, date_str
                    )
                    if pitching_candidate:
                        if batting_candidate is not None:
                            # player crossed both thresholds — keep the stronger line only
                            # again, this is basically a check only for shohei
                            if pitching_candidate['score'] > batting_candidate['score']:
                                # replace the batting entry we already appended
                                candidates[-1] = pitching_candidate
                            # else: keep batting entry as-is; pitching is weaker
                        else:
                            candidates.append(pitching_candidate)

    # sort descending by score, we get highest performances go to the top
    # lambda function lets us do this cleanly on a single line
    candidates.sort(key=lambda cand: cand['score'], reverse=True)

    print(f'[statlines] {len(candidates)} candidates above threshold across {len(box_scores)} games')

    return candidates[:n]


# scoring functions below to calcualte score lines

def _score_batting(player_data, stat, team_name, opponent, game_date):
    '''return a candidate dict for a batting line, or None if below threshold.

    we calcualte score across the weighted stats; anything under our chosen
    threshold is ignored so we don't pollute the top selection with a 1-for-4 bum
    '''
    score = (
        _BATTING_WEIGHTS['homeRuns']    * stat.get('homeRuns', 0) +
        _BATTING_WEIGHTS['hits']        * stat.get('hits', 0) +
        _BATTING_WEIGHTS['rbi']         * stat.get('rbi', 0) +
        _BATTING_WEIGHTS['runs']        * stat.get('runs', 0) +
        _BATTING_WEIGHTS['stolenBases'] * stat.get('stolenBases', 0)
    )

    if score < _BATTING_THRESHOLD:
        return None  # not a performance worth highlighting, skip out

    return {
        'player':   boxscore.player_name(player_data, default='unknown player'),
        'team':     team_name,
        'opponent': opponent,
        'date':     game_date,
        'category': 'batting',
        'line':     _format_batting_line(stat),
        'score':    score,
    }  # return a batter who did have a good day with the details around the player, game and statline


def _score_pitching(player_data, stat, team_name, opponent, game_date):
    '''return a candidate dict for a pitching line, or None if below threshold.

    inningsPitched comes back as a string like "6.1" where the digit after
    the decimal is *outs*, not tenths of an inning. 
    so 6.1 = 6 1/3 innings and 6.2 = 6 2/3 innings. 
    we'll convert to true float (e.g. 6.333) so the
    weight math is meaningful rather than treating .2 as two-tenths of an inning
    baseball is fun...
    '''
    innings_pitched_raw = stat.get('inningsPitched', '0.0')
    ip = boxscore.parse_innings_pitched(innings_pitched_raw)

    score = (
        _PITCHING_WEIGHTS['wins']           * stat.get('wins', 0) +
        _PITCHING_WEIGHTS['strikeOuts']     * stat.get('strikeOuts', 0) +
        _PITCHING_WEIGHTS['inningsPitched'] * ip +
        _PITCHING_WEIGHTS['earnedRuns']     * stat.get('earnedRuns', 0)
        # earnedRuns weight is negative so this subtracts, expalined at start but pretty clear
    )

    if score < _PITCHING_THRESHOLD:
        return None  # below the interesting threshhold 

    return {
        'player':   boxscore.player_name(player_data, default='unknown player'),
        'team':     team_name,
        'opponent': opponent,
        'date':     game_date,
        'category': 'pitching',
        'line':     _format_pitching_line(stat, ip),
        'score':    score,
    }  # return the pitcher who had a good day, with the game and team infos, plus statline


# formatting helpers functions

def _format_batting_line(stat):
    '''one-liner summary of a batting stat dict ready for the summary

    e.g. "3-4, 2 HR, 4 RBI, 1 SB" - a really good day!
    this keeps it compact enough for a bot message if using slack options
    we only include stolen bases and runs if they're nonzero values
    '''
    hits    = stat.get('hits', 0)
    at_bats = stat.get('atBats', 0)
    hr      = stat.get('homeRuns', 0)
    rbi     = stat.get('rbi', 0)
    runs    = stat.get('runs', 0)
    sb      = stat.get('stolenBases', 0)
    bb      = stat.get('baseOnBalls', 0)

    # start with the hits/at-bats as we always use that
    parts = [f'{hits}-{at_bats}']
    
    # add in the parts that are there if nonzero
    if hr:
        parts.append(f'{hr} HR')
    if rbi:
        parts.append(f'{rbi} RBI')
    if runs:
        parts.append(f'{runs} R')
    if sb:
        parts.append(f'{sb} SB')
    if bb:
        parts.append(f'{bb} BB')  # mention walks if the player has done enpugh to be included, but we don't weight them

    return ', '.join(parts)


def _format_pitching_line(stat, ip_float=None):
    '''one-liner summary of a pitching stat dict.

    e.g. "W, 7.0 IP, 9 K, 1 ER"
    we always show IP and K because those are main statlines for pitchers
    W and ER are conditional on having something to note
    '''
    # use already-parsed float as caller passes it, otherwise re-parse in case its missing
    # this avoids calling _parse_innings_pitched twice - we should get this already upsteam
    if ip_float is None:
        ip_float = boxscore.parse_innings_pitched(stat.get('inningsPitched', '0.0'))

    wins  = stat.get('wins', 0)
    ks    = stat.get('strikeOuts', 0)
    er    = stat.get('earnedRuns', 0)
    hits  = stat.get('hits', 0)
    bb    = stat.get('baseOnBalls', 0)

    # format innings to one decimal, but use the original string instead
    # showing "6.2" is more accurate for baseball that 6.6 
    ip_display = stat.get('inningsPitched', f'{ip_float:.1f}')

    parts = []
    if wins:
        parts.append('W')       # win goes first, thats the big news
    parts.append(f'{ip_display} IP')
    parts.append(f'{ks} K')  # always add innings and strike outs
    if er:
        parts.append(f'{er} ER')
    if hits:
        parts.append(f'{hits} H')
    if bb:
        parts.append(f'{bb} BB')
    #  add earned runs, hits and walks if they are there to add

    return ', '.join(parts)
