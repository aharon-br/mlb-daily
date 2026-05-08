'''
get_scores.py — gets yesterday's MLB games and box scores for the daily highlights bot.

this talks to the MLB stats API via the
statsapi library (totally open, shockingsly MLB just gives this stuff away)
everything here is read-only: grab the schedule for a given date, filter down
to games that actually finished, then pull box score data and play-by-play
for each game. downstream modules consume what we return here.

the statsapi library wraps https://statsapi.mlb.com/api/ — if you ever want to
look around the insanely long raw json yourself its all there
'''

import statsapi  # pip install MLB-StatsAPI — the unofficial python lib
from datetime import date, timedelta

# MLB Stats API Python wrapper: https://github.com/toddrob99/MLB-StatsAPI
# API base: https://statsapi.mlb.com/api/


''' game types we care about: R=regular season, P=wild card, F=division series,
D=league championship series, L=league championship (old code), W=world series.
we explicitly exclude E (spring training) and S (exhibition) so we don't get
cactus/grapefruit league junk bleeding in during march/april.'''
_VALID_GAME_TYPES = {'R', 'P', 'F', 'D', 'L', 'W'}

'''these are the status mlb uses for games that are fully done
'Final' is the normal case; 'Game Over' appears briefly right after the final out
before the official 'Final' change; 'Completed Early' covers rain-shortened games'''
_FINAL_STATUSES = {'Final', 'Game Over', 'Completed Early'}


def yesterday():
    '''returns the date object for yesterday — convenience wrapper so callers
    don't have to import datetime themselves.'''
    # timedelta(days=1) is the cleanest way to do "one day ago" without any
    # off-by-one risk from rolling your own arithmetic
    return date.today() - timedelta(days=1)


def games_for_date(target_date):
    '''get all completed, real-season games for a given date.

    target_date — a datetime.date object from yesterday()
    returns a list of game dicts straight from statsapi.schedule().

    each dict has keys like: game_id, game_date, status, game_type, etc.
    the statsapi library uses snake_case for all schedule dict keys.
    '''
    print(f'getting schedule for {target_date.isoformat()}')

    # statsapi.schedule() accepts date strings in YYYY-MM-DD format.
    # passing start_date == end_date gives us just that one day.
    try:
        raw_schedule = statsapi.schedule(start_date=target_date.isoformat(), end_date=target_date.isoformat())
    except Exception as error:
        print(f'statsapi.schedule() raised for {target_date.isoformat()}: {error}')
        return []

    if not raw_schedule:
        print(f'no games found in schedule for {target_date.isoformat()}')
        return []

    '''only games that are fully complete
    is highly unlikely but possible west coast games are still live
    don't pull in in-progress scores and post incomplete box scores
    statsapi.schedule() returns 'status' as a plain string (the detailedState).'''
    final_games = [game for game in raw_schedule if game.get('status') in _FINAL_STATUSES]

    '''filter only meaningful game types
    spring training (E) and exhibition (S) games show up in the schedule
    during march and april — we don't want the service adding info about a
    padres/padres intrasquad game from peoria
    the key is 'game_type' in statsapi schedule dicts (snake_case throughout).'''
    real_games = [game for game in final_games if game.get('game_type', 'R') in _VALID_GAME_TYPES]

    # sample game dict from statsapi.schedule():
    # {
    #   'game_id': 745458,
    #   'game_date': '2026-04-29',
    #   'game_type': 'R',
    #   'status': 'Final',
    #   'away_name': 'New York Yankees',
    #   'home_name': 'Boston Red Sox',
    #   'away_id': 147,
    #   'home_id': 111,
    #   'away_score': 3,
    #   'home_score': 5,
    #   'venue_name': 'Fenway Park',
    # }
    print(f'got {len(real_games)} completed games (of {len(raw_schedule)} scheduled)')
    return real_games


def box_score(game_pk):
    '''get the full box score data dict for a single game.

    game_pk is string game ID MLB assign
    returns a dict with 'home' and 'away' keys, each one has:
      - 'players': dict of player stats keyed by player ID string e.g. 'ID123456'
      - 'team': just {'id'}, see note below, the name and abbreviation are in box['teamInfo']['home'/'away']
      - 'teamStats': aggregated batting and pitching lines for the whole team
      - 'batters': list of player IDs in batting order
      - 'pitchers': list of player IDs who appeared
      - 'info': game info notes like weather, attendance, umps

    inningsPitched is a string like '6.1' where the decimal digit is OUTS (0/1/2),
    this is pretty normal in baseball parlance and any fan gets this
    but python isn't a baseball fan and does not get this so we need a function to conver that
    so '6.1' means 6⅓ innings, not 6.1. 
    we use _parse_innings_pitched()

    team abbreviation (like 'NYY') is not in box['home']['team'] — it lives in
    box['teamInfo']['home']['abbreviation']. 
    box['home']['team'] only has {'id'} which is not human friendly
    '''
    print(f'    getting box score for game {game_pk}')

    '''statsapi.boxscore_data() is a higher-level helper that parses the raw
    /game/{gamePk}/boxscore endpoint into something more python useable.
    it puts player stats into the roster structure so we dont have to form it back'''
    data = statsapi.boxscore_data(game_pk)

    '''abbreviated sample of what the return looks like
    {
      'home': {
        'team': {'id': 111},  # only 'id' — name/abbreviation are NOT here
        'teamStats': {
          'batting':  {'runs': 5, 'hits': 9, 'homeRuns': 2, 'atBats': 33,
                       'strikeOuts': 7, 'baseOnBalls': 3, 'rbi': 5, ...},
          'pitching': {'runs': 3, 'strikeOuts': 8, 'inningsPitched': '9.0',
                       'earnedRuns': 3, 'pitchesThrown': 142, 'strikes': 94, ...},
        },
        'batters':  [656305, 592518, ...],  # player IDs in batting order
        'pitchers': [453286, 669203, ...],
        'players': {
          'ID656305': {
            'person': {'id': 656305, 'fullName': 'Rafael Devers'},
            'position': {'abbreviation': '3B'},
            'stats': {
              'batting':  {'hits': 2, 'homeRuns': 1, 'rbi': 2, 'runs': 1,
                           'atBats': 4, 'strikeOuts': 1, 'baseOnBalls': 0, ...},
              # note: 'doubles'/'triples' do NOT appear in boxscore_data() player stats
              'pitching': {},
            },
          },
          ...
        },
      },
      'away': { ... same shape ... },
      'teamInfo': {
        'home': {'id': 111, 'abbreviation': 'BOS', 'teamName': 'Red Sox', 'shortName': 'Boston'},
        'away': {'id': 147, 'abbreviation': 'NYY', 'teamName': 'Yankees', 'shortName': 'NY Yankees'},
      },
    }'''

    # check that the object is what we expect in case the API returns bad data or
    # partial data for weird edge cases
    if 'home' not in data or 'away' not in data:
        print(f'    box score for {game_pk} missing home/away — got keys: {list(data.keys())}')

    return data


def get_play_by_play(game_pk):
    '''get the full play-by-play log for a game.

    game_pk — same game ID as box_score().
    returns the liveData.plays dict from the MLB live feed, which contains:
      - 'allPlays': list of every at-bat/event in order
      - 'currentPlay': the last play (useful for live games, mostly irrelevant here)
      - 'scoringPlays': list of indices into allPlays where a run scored
      - 'playsByInning': plays grouped by inning

    used by downstream rarity-detection logic — e.g. to spot no-hitters,
    perfect games, consecutive home runs, rare at-bat outcomes, etc.
    the full play log is verbose (big games can be 150+ plays), so we only
    get it when we actually need to dig into the sequence of events.
    '''
    print(f'    getting play-by-play for game {game_pk}')

    # statsapi.get() is the low-level escape hatch that hits any endpoint by name.
    # the 'game' endpoint is /api/v1.1/game/{gamePk}/feed/live and returns the
    # full live feed json — batting, pitching, fielding events, pitch-by-pitch, all of it.
    # we only pull liveData.plays because the full feed also includes box score
    # data we already have, and the json can get quite large.
    raw_play_by_play = statsapi.get('game', {'gamePk': game_pk})

    # sample plays shape (abbreviated):
    # {
    #   'allPlays': [
    #     {
    #       'result': {'event': 'Strikeout', 'description': 'Luis Arraez strikes out swinging.'},
    #       'about':  {'inning': 1, 'halfInning': 'top', 'isComplete': True},
    #       'matchup': {
    #         'batter':  {'id': 650333, 'fullName': 'Luis Arraez'},
    #         'pitcher': {'id': 453286, 'fullName': 'Chris Sale'},
    #       },
    #       'pitchIndex': [0, 1, 2],  # indices into playEvents; len = pitch count for AB
    #     },
    #     ...
    #   ],
    #   'playsByInning': [
    #     {'num': 1, 'top': [0, 1, 2], 'bottom': [3, 4, 5]},  # indices into allPlays
    #     ...
    #   ],
    # }

    # get into the nested data — the api always has this format returned for finished games
    try:
        plays = raw_play_by_play['liveData']['plays']
    except KeyError:
        # return an empty object so we don't have to check for None all the time
        print(f'    no liveData.plays found for game {game_pk}')
        plays = {}

    return plays


def get_yesterday():
    '''gets everything for yesterday's games and combines up

    returns a list of dicts with:
      - 'game': the schedule dict so gamePk, teams, status, etc
      - 'box_score': the boxscore_data dict so player stats etc
      - 'play_by_play': the liveData.plays dict so all at-bats in sequence, huge data

    this is the main start point for the rest of the work
    most callers should will call this and iterate over what comes back
    and wont call each function in here on its own
    '''
    target_date = yesterday()
    print(f'getting data for {target_date.isoformat()}')

    games = games_for_date(target_date)

    if not games:
        # totally normal on off-days - mondays in april, the all-star break, etc.
        print('no completed games found — this is expected on off-days')
        return []

    results = []
    for game in games:
        game_pk = game['game_id']
        away_name = game.get('away_name', 'away')
        home_name = game.get('home_name', 'home')
        print(f'processing: {away_name} @ {home_name} (pk={game_pk})')

        box = box_score(game_pk)
        plays = get_play_by_play(game_pk)

        results.append({
            'game': game,
            'box_score': box,
            'play_by_play': plays,
        })

    print(f'complete, got all data for {len(results)} games')
    return results
