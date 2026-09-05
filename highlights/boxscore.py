'''
boxscore.py — shared accessors for the statsapi box score / play-by-play shapes.

every module that consumes `statsapi.boxscore_data()` output was re-implementing
the same traversal: loop over ('away', 'home'), dig the roster list out of
box[side], pull player stats out of box[side]['players']['ID{pid}']['stats'],
and read the team name out of box['teamInfo'][side] (it is NOT in box[side]['team'],
which only carries {'id'}). the helpers here own that shape in one place so the
detectors upstream read as baseball logic instead of dict spelunking.

same story for play-by-play: box['play_by_play'] carries 'allPlays' plus
'playsByInning' with index lists per half-inning, and four different detectors
were each resolving those indices by hand.
'''

from collections import namedtuple

# statsapi keys both sides at the top level of the box dict
SIDES = ('away', 'home')


# ---- team / player accessors ------------------------------------------------

def team_info(box, side):
    '''the teamInfo entry for a side — {'id', 'abbreviation', 'teamName', ...}.'''
    return box.get('teamInfo', {}).get(side, {})


def team_name(box, side, default='Unknown'):
    '''human-readable club name, e.g. "Red Sox".'''
    return team_info(box, side).get('teamName', default)


def team_abbrev(box, side, default=''):
    '''StatsAPI team abbreviation, e.g. "BOS".'''
    return team_info(box, side).get('abbreviation', default)


def opponent_side(side):
    '''the other half of the box score.'''
    return 'away' if side == 'home' else 'home'


def player_entry(box, side, pid):
    '''the players dict entry for a player id, or {} when absent.'''
    return box.get(side, {}).get('players', {}).get(f'ID{pid}', {})


def player_name(player, pid=None, default=None):
    '''full name for a player entry, falling back to "Player {pid}".'''
    fallback = default if default is not None else f'Player {pid}'
    return player.get('person', {}).get('fullName', fallback)


PlayerLine = namedtuple('PlayerLine', 'side team_name team_abbrev pid player name stats')


def iter_player_lines(box, roster, category, require_stats=True):
    '''walk both sides of a box score and yield one PlayerLine per player.

    roster   — 'batters' or 'pitchers' (the id lists statsapi provides)
    category — 'batting' or 'pitching' stat block to attach
    require_stats — skip players whose stat block is empty (a defensive sub
                    who never came to the plate, say)
    '''
    for side in SIDES:
        side_data = box.get(side, {})
        name = team_name(box, side)
        abbrev = team_abbrev(box, side)

        for pid in side_data.get(roster, []):
            player = player_entry(box, side, pid)
            stats = player.get('stats', {}).get(category, {})
            if require_stats and not stats:
                continue
            yield PlayerLine(
                side=side,
                team_name=name,
                team_abbrev=abbrev,
                pid=pid,
                player=player,
                name=player_name(player, pid),
                stats=stats,
            )


def iter_position_player_pitchers(box):
    '''yield a PlayerLine for every non-pitcher who took the mound.

    a player counts when they appear in the pitchers list but neither their
    primary position nor any of their listed positions is 'P' — that second
    check keeps two-way players (ohtani) out of the results.

    the primary position is on the PlayerLine's `player` entry; callers read it
    via position_abbrev().
    '''
    for line in iter_player_lines(box, 'pitchers', 'pitching', require_stats=False):
        primary_pos = position_abbrev(line.player)
        all_pos = [
            pos.get('abbreviation', '')
            for pos in line.player.get('allPositions', [])
        ]
        if primary_pos == 'P' or 'P' in all_pos:
            continue
        yield line


def position_abbrev(player):
    '''primary listed position for a player entry, e.g. "RF".'''
    return player.get('position', {}).get('abbreviation', '')


def team_stats(box, side, category):
    '''aggregated team batting/pitching line for a side.'''
    return box.get(side, {}).get('teamStats', {}).get(category, {})


# ---- innings pitched --------------------------------------------------------

def parse_innings_pitched(ip_str):
    '''convert statsapi's "6.1" string to a real fractional float (6.333).

    statsapi encodes outs as the decimal digit, so we split on the dot, grab
    the outs part, and divide by 3 to get the true inning fraction. baseball is
    fun. a malformed value returns 0.0 so callers can keep going.
    '''
    try:
        parts = str(ip_str).split('.')
        full_innings = int(parts[0])
        outs = int(parts[1]) if len(parts) > 1 else 0
        return full_innings + outs / 3.0
    except (ValueError, IndexError):
        print(f'[boxscore] warning: could not parse inningsPitched value: {ip_str!r}')
        return 0.0


# ---- play-by-play -----------------------------------------------------------

class HalfInning(namedtuple('HalfInning', 'num half indices plays')):
    '''one half-inning of play-by-play, with its plays already resolved.

    `indices` is the raw index list from playsByInning; `plays` is those
    indices looked up in allPlays (out-of-range indices dropped), so a caller
    that cares about a complete half-inning can compare the two lengths.
    '''

    __slots__ = ()

    @property
    def batting_side(self):
        '''top half = away team bats, bottom = home.'''
        return 'away' if self.half == 'top' else 'home'

    @property
    def pitching_side(self):
        return opponent_side(self.batting_side)

    @property
    def events(self):
        '''result event names in plate-appearance order.'''
        return [play.get('result', {}).get('event', '') for play in self.plays]


def iter_half_innings(box):
    '''yield a HalfInning for every top/bottom half in the play-by-play.

    silently yields nothing when box['play_by_play'] is missing — the box score
    alone doesn't say which inning anything happened in, so the callers all
    degrade to "no patterns found."
    '''
    plays_data = box.get('play_by_play') or {}
    all_plays = plays_data.get('allPlays', [])

    for inning_data in plays_data.get('playsByInning', []):
        num = inning_data.get('num', '?')
        for half in ('top', 'bottom'):
            indices = inning_data.get(half, [])
            plays = [all_plays[pi] for pi in indices if pi < len(all_plays)]
            yield HalfInning(num=num, half=half, indices=indices, plays=plays)


def final_half_inning(box, half='bottom'):
    '''the requested half of the last inning played, or None.

    only that half-inning can contain a walk-off.
    '''
    last = None
    for half_inning in iter_half_innings(box):
        if half_inning.half == half:
            last = half_inning
    return last


def is_immaculate(half_inning):
    '''True when a half-inning was 3 strikeouts on exactly 9 pitches.

    three plate appearances, all strikeouts, each on exactly 3 pitches
    (pitchIndex holds one entry per pitch of the at-bat).
    '''
    if len(half_inning.indices) != 3 or len(half_inning.plays) != 3:
        return False
    if not all(event == 'Strikeout' for event in half_inning.events):
        return False
    return all(len(play.get('pitchIndex', [])) == 3 for play in half_inning.plays)


def matchup_name(play, role, default=''):
    '''full name of the batter or pitcher in a play's matchup.'''
    return play.get('matchup', {}).get(role, {}).get('fullName', default)


def ordinal(num):
    '''return the ordinal string for an integer, e.g. 1 -> "1st", 3 -> "3rd".'''
    num = int(num)
    suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(num % 10, 'th')
    if 11 <= num % 100 <= 13:
        suffix = 'th'
    return f'{num}{suffix}'
