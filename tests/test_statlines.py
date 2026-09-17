'''unit tests for the stat line scorer.

covers the weighting math, the batting/pitching thresholds, the innings-pitched
"outs as decimal" parsing, the two-way (ohtani) de-duplication, and the
one-liner formatting helpers.
'''

from highlights import statlines


def _batting_box(stat, side='home', team='Red Sox', opponent='Yankees',
                 name='Rafael Devers'):
    other = 'away' if side == 'home' else 'home'
    return {
        'teamInfo': {
            side: {'teamName': team},
            other: {'teamName': opponent},
        },
        side: {
            'players': {
                'ID1': {'person': {'fullName': name}, 'stats': {'batting': stat}},
            },
        },
        other: {'players': {}},
    }


def test_parse_innings_pitched_treats_decimal_as_outs():
    assert statlines._parse_innings_pitched('6.0') == 6.0
    assert statlines._parse_innings_pitched('6.1') == 6 + 1 / 3
    assert statlines._parse_innings_pitched('6.2') == 6 + 2 / 3
    assert statlines._parse_innings_pitched('7') == 7.0


def test_parse_innings_pitched_bad_input_returns_zero():
    assert statlines._parse_innings_pitched('not-an-inning') == 0.0
    assert statlines._parse_innings_pitched(None) == 0.0


def test_score_batting_below_threshold_is_dropped():
    # 1-for-4 with nothing else = 1.0, well under the threshold of 4
    assert statlines._score_batting(
        {'person': {'fullName': 'Nobody'}}, {'hits': 1, 'atBats': 4},
        'Red Sox', 'Yankees', '2026-04-29',
    ) is None


def test_score_batting_applies_weights():
    stat = {'hits': 3, 'atBats': 4, 'homeRuns': 2, 'rbi': 4, 'runs': 2, 'stolenBases': 1}
    candidate = statlines._score_batting(
        {'person': {'fullName': 'Rafael Devers'}}, stat,
        'Red Sox', 'Yankees', '2026-04-29',
    )
    # 4*2 + 1*3 + 1.5*4 + 0.5*2 + 2*1 = 20.0
    assert candidate['score'] == 20.0
    assert candidate['category'] == 'batting'
    assert candidate['player'] == 'Rafael Devers'
    assert candidate['team'] == 'Red Sox'
    assert candidate['opponent'] == 'Yankees'
    assert candidate['date'] == '2026-04-29'


def test_score_pitching_applies_weights_with_fractional_innings():
    stat = {'wins': 1, 'strikeOuts': 9, 'inningsPitched': '6.1', 'earnedRuns': 1}
    candidate = statlines._score_pitching(
        {'person': {'fullName': 'Chris Sale'}}, stat,
        'Red Sox', 'Yankees', '2026-04-29',
    )
    expected = 3.0 + 0.5 * 9 + 1.5 * (6 + 1 / 3) - 2.0
    assert candidate['score'] == expected
    assert candidate['category'] == 'pitching'


def test_score_pitching_below_threshold_is_dropped():
    # 2 innings, 1 K, 3 ER -> 3.0 + 0.5 - 6.0 = -2.5
    assert statlines._score_pitching(
        {'person': {'fullName': 'Mop Up'}},
        {'inningsPitched': '2.0', 'strikeOuts': 1, 'earnedRuns': 3},
        'Red Sox', 'Yankees', '2026-04-29',
    ) is None


def test_top_performances_sorts_and_limits():
    boxes = [
        _batting_box({'hits': 2, 'atBats': 4, 'homeRuns': 1}, name='Modest'),
        _batting_box({'hits': 4, 'atBats': 5, 'homeRuns': 3, 'rbi': 6}, name='Monster'),
    ]
    top = statlines.top_performances(boxes, n=1, game_date='2026-04-29')
    assert len(top) == 1
    assert top[0]['player'] == 'Monster'


def test_top_performances_uses_box_date_when_no_game_date():
    box = _batting_box({'hits': 3, 'atBats': 4, 'homeRuns': 1})
    box['gameDate'] = '2026-05-02'
    top = statlines.top_performances([box])
    assert top[0]['date'] == '2026-05-02'


def test_top_performances_falls_back_to_unknown_team_names():
    box = _batting_box({'hits': 3, 'atBats': 4, 'homeRuns': 1})
    del box['teamInfo']
    top = statlines.top_performances([box], game_date='2026-04-29')
    assert top[0]['team'] == 'unknown team'
    assert top[0]['opponent'] == 'unknown opponent'


def _two_way_box(batting, pitching):
    return {
        'teamInfo': {'home': {'teamName': 'Dodgers'}, 'away': {'teamName': 'Padres'}},
        'home': {
            'players': {
                'ID660271': {
                    'person': {'fullName': 'Shohei Ohtani'},
                    'stats': {'batting': batting, 'pitching': pitching},
                },
            },
        },
        'away': {'players': {}},
    }


def test_two_way_player_keeps_only_the_stronger_line():
    strong_pitching = {'wins': 1, 'strikeOuts': 12, 'inningsPitched': '8.0', 'earnedRuns': 0}
    weak_batting = {'hits': 2, 'atBats': 4, 'homeRuns': 1}
    top = statlines.top_performances(
        [_two_way_box(weak_batting, strong_pitching)], game_date='2026-04-29')
    assert len(top) == 1
    assert top[0]['category'] == 'pitching'


def test_two_way_player_keeps_batting_when_batting_is_stronger():
    weak_pitching = {'wins': 1, 'strikeOuts': 5, 'inningsPitched': '5.0', 'earnedRuns': 2}
    strong_batting = {'hits': 4, 'atBats': 5, 'homeRuns': 3, 'rbi': 6, 'runs': 3}
    top = statlines.top_performances(
        [_two_way_box(strong_batting, weak_pitching)], game_date='2026-04-29')
    assert len(top) == 1
    assert top[0]['category'] == 'batting'


def test_pitching_only_player_is_added():
    box = _two_way_box({}, {'wins': 1, 'strikeOuts': 10, 'inningsPitched': '7.0',
                            'earnedRuns': 1})
    top = statlines.top_performances([box], game_date='2026-04-29')
    assert [perf['category'] for perf in top] == ['pitching']


def test_format_batting_line_omits_zero_categories():
    assert statlines._format_batting_line({'hits': 1, 'atBats': 4}) == '1-4'
    assert statlines._format_batting_line({
        'hits': 3, 'atBats': 4, 'homeRuns': 2, 'rbi': 4, 'runs': 2,
        'stolenBases': 1, 'baseOnBalls': 1,
    }) == '3-4, 2 HR, 4 RBI, 2 R, 1 SB, 1 BB'


def test_format_pitching_line_shows_original_innings_string():
    line = statlines._format_pitching_line(
        {'wins': 1, 'inningsPitched': '6.2', 'strikeOuts': 9, 'earnedRuns': 1,
         'hits': 4, 'baseOnBalls': 2},
        ip_float=6 + 2 / 3,
    )
    assert line == 'W, 6.2 IP, 9 K, 1 ER, 4 H, 2 BB'


def test_format_pitching_line_reparses_when_no_float_given():
    assert statlines._format_pitching_line({'inningsPitched': '5.0', 'strikeOuts': 3}) == \
        '5.0 IP, 3 K'
