'''unit tests for the shared box score / formatting helpers.

these pin the behaviour the detectors in rarity.py and statlines.py now rely
on: which players get yielded, how innings pitched are decoded, and how a
half-inning is judged immaculate.

run from the repo root with: python -m pytest tests/test_boxscore.py
'''

import pytest

from highlights import boxscore, formatting


def _player(name, pos='RF', all_pos=None, batting=None, pitching=None):
    return {
        'person': {'fullName': name},
        'position': {'abbreviation': pos},
        'allPositions': [{'abbreviation': p} for p in (all_pos or [pos])],
        'stats': {'batting': batting or {}, 'pitching': pitching or {}},
    }


BOX = {
    'teamInfo': {
        'away': {'abbreviation': 'LAD', 'teamName': 'Dodgers'},
        'home': {'abbreviation': 'BOS', 'teamName': 'Red Sox'},
    },
    'away': {
        'batters': [1, 2],
        'pitchers': [3, 4],
        'players': {
            'ID1': _player('Hitter One', batting={'hits': 2, 'atBats': 4}),
            'ID2': _player('Bench Bat'),  # no stats — never came to the plate
            'ID3': _player('Ace Arm', pos='P', pitching={'inningsPitched': '6.1'}),
            'ID4': _player('Utility Man', pos='2B', pitching={'inningsPitched': '1.0'}),
        },
        'teamStats': {'batting': {'runs': 6}, 'pitching': {'runs': 0}},
    },
    'home': {
        'batters': [],
        'pitchers': [5],
        'players': {
            'ID5': _player('Two Way', pos='DH', all_pos=['DH', 'P'],
                           pitching={'inningsPitched': '2.0'}),
        },
        'teamStats': {'batting': {'runs': 0}, 'pitching': {'runs': 6}},
    },
}


def test_team_accessors_read_teaminfo_not_the_team_dict():
    assert boxscore.team_name(BOX, 'home') == 'Red Sox'
    assert boxscore.team_abbrev(BOX, 'away') == 'LAD'
    assert boxscore.team_name({}, 'home') == 'Unknown'
    assert boxscore.opponent_side('home') == 'away'


def test_iter_player_lines_skips_empty_stat_blocks():
    names = [line.name for line in boxscore.iter_player_lines(BOX, 'batters', 'batting')]
    assert names == ['Hitter One']  # 'Bench Bat' has no batting stats

    with_empties = [
        line.name
        for line in boxscore.iter_player_lines(BOX, 'batters', 'batting', require_stats=False)
    ]
    assert with_empties == ['Hitter One', 'Bench Bat']


def test_iter_player_lines_carries_team_context():
    line = next(boxscore.iter_player_lines(BOX, 'batters', 'batting'))
    assert (line.side, line.team_name, line.team_abbrev) == ('away', 'Dodgers', 'LAD')
    assert line.stats == {'hits': 2, 'atBats': 4}


def test_position_player_pitchers_excludes_pitchers_and_two_way_players():
    names = [line.name for line in boxscore.iter_position_player_pitchers(BOX)]
    assert names == ['Utility Man']  # 'Ace Arm' is a P, 'Two Way' lists P in allPositions


def test_team_stats():
    assert boxscore.team_stats(BOX, 'away', 'pitching') == {'runs': 0}
    assert boxscore.team_stats(BOX, 'home', 'batting') == {'runs': 0}


@pytest.mark.parametrize('raw, expected', [
    ('6.0', 6.0),
    ('6.1', 6 + 1 / 3),
    ('6.2', 6 + 2 / 3),
    ('7', 7.0),
    ('nonsense', 0.0),  # malformed values degrade to zero rather than raising
])
def test_parse_innings_pitched_treats_the_decimal_as_outs(raw, expected):
    assert boxscore.parse_innings_pitched(raw) == pytest.approx(expected)


def _k(pitches=3):
    return {'result': {'event': 'Strikeout'}, 'pitchIndex': list(range(pitches)),
            'matchup': {'pitcher': {'fullName': 'Ace Arm'}}}


def _pbp(plays, by_inning):
    return {'allPlays': plays, 'playsByInning': by_inning}


def test_iter_half_innings_resolves_indices_and_sides():
    box = {'play_by_play': _pbp([_k(), _k(), _k()], [{'num': 1, 'top': [0, 1], 'bottom': [2]}])}
    halves = list(boxscore.iter_half_innings(box))

    assert [(h.num, h.half, len(h.plays)) for h in halves] == [(1, 'top', 2), (1, 'bottom', 1)]
    assert halves[0].batting_side == 'away' and halves[0].pitching_side == 'home'
    assert halves[1].batting_side == 'home' and halves[1].pitching_side == 'away'
    assert halves[0].events == ['Strikeout', 'Strikeout']


def test_iter_half_innings_without_play_by_play_yields_nothing():
    assert list(boxscore.iter_half_innings({})) == []


def test_is_immaculate_requires_three_ks_on_nine_pitches():
    immaculate = _pbp([_k(), _k(), _k()], [{'num': 3, 'top': [0, 1, 2], 'bottom': []}])
    top = next(iter(boxscore.iter_half_innings({'play_by_play': immaculate})))
    assert boxscore.is_immaculate(top)

    # a four-pitch strikeout in the mix disqualifies the inning
    sloppy = _pbp([_k(), _k(4), _k()], [{'num': 3, 'top': [0, 1, 2], 'bottom': []}])
    top = next(iter(boxscore.iter_half_innings({'play_by_play': sloppy})))
    assert not boxscore.is_immaculate(top)


def test_final_half_inning_picks_the_last_one():
    box = {'play_by_play': _pbp(
        [_k(), _k()], [{'num': 8, 'top': [], 'bottom': [0]}, {'num': 9, 'top': [], 'bottom': [1]}]
    )}
    final = boxscore.final_half_inning(box, half='bottom')
    assert final.num == 9


@pytest.mark.parametrize('num, expected', [
    (1, '1st'), (2, '2nd'), (3, '3rd'), (4, '4th'),
    (11, '11th'), (12, '12th'), (13, '13th'), (21, '21st'),
])
def test_ordinal(num, expected):
    assert boxscore.ordinal(num) == expected


def test_truncate_stays_within_the_limit_and_marks_the_cut():
    assert formatting.truncate('short', 50, '...') == 'short'

    cut = formatting.truncate('word ' * 50, 40, '...')
    assert len(cut) == 40 and cut.endswith('...')

    on_word = formatting.truncate('alpha bravo charlie', 12, '...', word_boundary=True)
    assert on_word == 'alpha...'


def test_event_line_appends_the_since_date_only_when_present():
    assert formatting.event_line({'description': 'a cycle', 'since': None}) == 'a cycle'
    assert formatting.event_line({'description': 'a cycle', 'since': '2014-05-01'}) == (
        'a cycle — _last seen: 2014-05-01_'
    )
