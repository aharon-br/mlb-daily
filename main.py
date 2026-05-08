'''
main.py — main run point for the MLB daily highlights bot

GitHub Actions runs this on a cron schedule via .github/workflows/daily-highlights.yml —
non-zero exit code signals failure back to the Actions runner

you can run locally if you want:

-- python main.py

 -- env vars:
  SLACK_WEBHOOK_URL     — required unless POST_TO_SLACK=false
  ANTHROPIC_API_KEY     — required only if AI_SUMMARY=true (passed through to
                          narrative.write_summary; the module reads it directly)
  AI_SUMMARY            — optional; set to 'true' to call the AI narrative step.
                          defaults to false
  POST_TO_SLACK         — optional; set to 'false' to skip the slack post.
                          the recap file is always written regardless of this flag,
                          as long as there were games to report (off-days exit early)
'''

import os
import sys
from datetime import date, timedelta

# highlights is our set of local packages — each sub-module owns one concern
from highlights import get_scores     # talks to the MLB stats API
from highlights import statlines      # builds per-player stat lines
from highlights import rarity         # detects rare / notable events
from highlights import narrative      # calls the AI to write the story
from highlights import slack as slack_send  # posts to slack
from highlights import recaps         # writes the local daily recap file


# ---- helper functions

def _filter_final_games(games):
    '''keep only games that are fully done.

    the MLB stats API uses a bunch of status codes to mean "this game is over."
    we want all of them so we don't silently drop rainouts, so completed early
    or extra-inning marathons that dragged past midnight

    anything still in progress, postponed, or scheduled gets dropped
    we can't build a box score from an incomplete game.
    '''
    # Final, Game Over and Completed Early are the three temrinal status
    # statsapi.schedule() returns 'status' as a plain string (the detailedState),
    # not a nested dict — so we compare directly against the string value.
    done_codes = {'Final', 'Game Over', 'Completed Early'}

    filtered = [
        game for game in games
        if game.get('status') in done_codes
    ]

    skipped = len(games) - len(filtered)
    if skipped:
        # useful to know when there were postponements or live games mixed in
        print(f'skipped {skipped} non-final game(s)')

    return filtered


# --- main 

def main():
    '''run the daily highlights service and save the the markdown report

    reads AI_SUMMARY and POST_TO_SLACK from the environment to decide whether
    to call the narrative step and whether to fire the slack webhook.
    keeps the default run cheap and credential-free.
    '''

    # figure out which calendar date we want — yesterday in local time.
    # the workflow fires at 13:00 UTC (9 AM ET), which is long after
    # all west coast games could get done. "yesterday" is pretty clear at that point
    target = date.today() - timedelta(days=1)
    print(f'getting games for {target}')

    # read the optionals once here so the prints below are accurate
    ai_summary = os.environ.get('AI_SUMMARY', 'false').lower() == 'true'
    print(f'ai_summary={ai_summary}')
    post_to_slack = os.environ.get('POST_TO_SLACK', 'true').lower() == 'true'
    print(f'post_to_slack={post_to_slack}')

    # 1. get the list of games that happened yesterday.
    # get_scores.games_for_date returns a list of game dicts
    # from the MLB schedule endpoint its mainly just to know yesterdays gamePks
    all_games = get_scores.games_for_date(target)

    if not all_games:
        # totally normal during the off-season, all-star break, etc for nothing
        print('no games found — likely an off day')
        return

    # drop anything that didn't reach a terminal state; we only want
    # final box scores (notated more on _filter_final_games)
    games = _filter_final_games(all_games)
    print(f'{len(games)} final game(s) out of {len(all_games)} scheduled')

    if not games:
        # games were scheduled but none finished - pretty rare, 9/11 level event
        print('all games postponed or still in progress — nothing to post')
        return

    # 2. get full box scores + play-by-play for every finished game
    # box scores are way bigger payloads — batting lines, pitching lines, etc
    # play-by-play is needed for pitch-level checks like the immaculate inning
    # we include play-by-play into each box dict so rarity checks can stay self-contained.
    print(f'getting {len(games)} box scores')
    box_scores = []
    for game in games:
        # gamePk is the MLB stats API's  integer game identifier - unique across all time
        box = get_scores.box_score(game['game_id'])
        box['play_by_play'] = get_scores.get_play_by_play(game['game_id'])
        box_scores.append(box)

    # 3. make the top-performer stat lines.
    # statlines.top_performances ranks every individual batting/pitching
    # line by a score of how _hard_ something is and returns the top n
    # n=8 keeps it simple enough highlight the most interesting
    print('calculating top performances...')
    top_lines = statlines.top_performances(box_scores, n=8, game_date=target.isoformat())
    print(f'found {len(top_lines)} top line(s)')

    # step 4: hunt for rare events.
    # rarity.find_rare_events scans play-by-play data for things like
    # no-hitters, perfect games, cycles, 4-HR games, immaculate innings.
    # most days this returns an empty list — that's fine.
    print('scanning for rare events...')
    rare_events = rarity.find_rare_events(box_scores, target)
    if rare_events:
        # log the names so they're searchable in the Actions run log
        event_names = [event.get('label', 'unknown') for event in rare_events]
        print(f'rare events found: {event_names}')
    else:
        print('no rare events today')

    # step 5: ask the model to write the narrative.
    # narrative.write_summary takes the verified stat lines and rare events
    # and returns a short, opinionated blurb — beat-writer voice, not recitation.
    # skipping this when ai_summary=false lets us post raw data without needing
    # any AI API credentials, which is handy for dry runs and testing the pipeline.
    print('generating narrative...')
    summary = narrative.write_summary(
        date=target,
        games=games,
        top_lines=top_lines,
        rare_events=rare_events,
        ai_summary=ai_summary,
    )

    # step 6: post to slack.
    # slack_send.post formats everything into slack blocks and fires the webhook.
    # keeping the send separate from the narrative lets us reuse
    # narrative.write_summary in tests without a slack side-effect.
    if post_to_slack:
        print('posting to slack...')
        slack_send.post(summary, top_lines, rare_events)
    else:
        print('slack disabled — skipping post')

    # step 7: write the daily recap file.
    # always runs regardless of post_to_slack — it's the local record.
    # the Actions workflow commits recaps/ alongside the season ledger each day.
    print('writing daily recap...')
    recaps.write_recap(
        date=target,
        games=games,
        top_lines=top_lines,
        rare_events=rare_events,
        summary=summary,
    )

    print('done')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'fatal error: {exc}')
        sys.exit(1)
