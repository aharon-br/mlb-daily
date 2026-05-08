# mlb-daily

a github actions bot that runs every morning, pulls the previous day's mlb games, finds the best stat lines and rare/historic moments, optionally has claude write a narrative, and posts it all to slack.

no servers, no cloud infrastructure — just a scheduled workflow, the free MLB Stats API, and a parquet file sitting in the repo.

## what it does

```
GitHub Actions (0 13 * * * UTC — 9 AM ET)
  └── MLB Stats API (get_scores.py)    — pulls schedule + box scores, no auth needed
  └── stat line scorer (statlines.py)  — scores every batting/pitching line, returns top N
  └── rarity engine (rarity.py)
        ├── Tier 1: box score checks (cycle, no-hitter, multi-HR) — instant, no I/O
        ├── Tier 2: season ledger in data/ (hit streaks, season firsts)
        └── Tier 3: retrosheet corpus parquet (historical "first since X" facts)
  └── Claude via Anthropic API (narrative.py)  — optional, writes prose around verified facts
  ├── recaps/{year}.md                 — always written; newest entry prepended at top
  └── Slack webhook (slack.py)         — Block Kit formatter + post (optional, see POST_TO_SLACK)
```

on off-days or full-postponement days it writes nothing and exits cleanly.

## why the rarity engine is separate from the AI

claude (and gemini, and every other llm) has a tendency to hallucinate facts like "first time since 1947" if you let it do history lookups on its own. confident-sounding nonsense.

the rarity engine in `rarity.py` is fully deterministic. it checks actual box score data, queries a parquet file of retrosheet events going back to 1918, and only emits an event if it can verify the claim. claude then writes prose *around* those verified facts — the system prompt explicitly tells it to use only the facts in the JSON it's been given, never invent stats or records, and if `rare_events` is empty, write accordingly.

this is the key architectural decision. the model's job is narration, not research.

## project layout

```
main.py                       — entrypoint, orchestrates the whole pipeline
requirements.txt              — python deps

highlights/
  get_scores.py               — MLB Stats API wrapper (statsapi library, no auth needed)
  statlines.py                — scores batting/pitching lines, returns top N performers
  rarity.py                   — the rarity engine (Tier 1/2/3, described below)
  corpus.py                   — offline ETL to build the retrosheet historical index
  narrative.py                — calls claude via anthropic SDK, returns the recap text
  slack.py                    — formats Block Kit blocks and posts to the webhook

.github/workflows/
  daily-highlights.yml        — the main scheduled workflow (runs at 9 AM ET)
  update-corpus.yml           — manual workflow to rebuild the retrosheet parquet

data/
  season-ledger-{year}.json   — tier 2 ledger, committed back after each run

recaps/
  {year}.md                   — daily recap markdown files, one per year (newest entry at top)

retrosheet-patterns.parquet   — tier 3 corpus, lives in the repo root
```

## setup

**1. fork or clone the repo**

```bash
git clone https://github.com/your-username/mlb-daily.git
cd mlb-daily
```

**2. create a slack app**

go to [api.slack.com/apps](https://api.slack.com/apps), create a new app "from a manifest," and use something like:

```yaml
display_information:
  name: mlb-daily
features:
  bot_user:
    display_name: mlb-daily
oauth_config:
  scopes:
    bot:
      - incoming-webhook
settings:
  org_deploy_enabled: false
  socket_mode_enabled: false
  token_rotation_enabled: false
```

install it to your workspace and pick the channel you want it posting to. you'll get an incoming webhook URL — that's all you need, no bot token.

**3. add github secrets**

go to your repo → settings → secrets and variables → actions, and add:

| secret | required | what it is |
|--------|----------|-----------|
| `SLACK_WEBHOOK_URL` | no* | incoming webhook URL from your slack app |
| `ANTHROPIC_API_KEY` | no | anthropic API key — only needed if you want the claude summary |

*`SLACK_WEBHOOK_URL` is only required if `POST_TO_SLACK` is `"true"` (the default). the recap file is always written regardless.

**4. configure ai summary and slack posting (both optional)**

two env vars in `.github/workflows/daily-highlights.yml` control the optional outputs:

```yaml
- name: Run bot
  env:
    SLACK_WEBHOOK_URL: ${{ secrets.SLACK_WEBHOOK_URL }}
    ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
    AI_SUMMARY: "true"      # set to "false" to skip the claude narrative
    POST_TO_SLACK: "true"   # set to "false" to skip the slack post
```

`AI_SUMMARY` defaults to `"false"` — set it to `"true"` if you have an anthropic API key and want the narrative prose. (The workflow hardcodes it to `"true"`, so you only hit the default when running locally without setting the var.)

`POST_TO_SLACK` defaults to `"true"` — set it to `"false"` to run the whole pipeline silently. the recap file is still written to `recaps/{year}.md` and committed back to the repo either way, so you can use slack-off mode to build up a history without spamming a channel.

**5. enable actions**

if you forked the repo, github may have disabled workflows — go to the actions tab and enable them. the bot will start firing daily at 9 AM ET.

## running locally

```bash
pip install -r requirements.txt
python main.py
```

required env vars for a full local run:

```
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
ANTHROPIC_API_KEY=sk-ant-...         # optional, needed only if AI_SUMMARY=true
AI_SUMMARY=true                      # optional, defaults to false
POST_TO_SLACK=true                   # optional, defaults to true — set to false to skip the post
```

for local testing without posting to slack, set `POST_TO_SLACK=false`. the pipeline still runs and writes the recap file — useful for smoke-testing without spamming the channel.

## recap files

every run writes a daily entry to `recaps/{year}.md` in the repo — regardless of whether slack posting is on. the workflow commits this file back after each run, the same way it commits the season ledger.

entries are prepended at the top of the file so the most recent game is always first. each entry includes the narrative (if `AI_SUMMARY` is on), the top stat lines, and any rare events the rarity engine found.

this doubles as a browsable archive. if you want to look back at what the bot found on a specific day, just open the year file on GitHub — no database, no log aggregator, just markdown in the repo.

## the rarity tiers

**tier 1 — single-game, from the box score**

computed right from the box score payload that `get_scores.py` already pulled. no extra i/o. checks for: cycles, no-hitters, perfect games, 3+ HR games, position player pitching, immaculate innings, double-digit strikeout games, and shutouts. runs instantly on every game.

**tier 2 — season ledger**

a small JSON file (`data/season-ledger-{year}.json`) that tracks ongoing state across the whole season: hitting streaks, season-first no-hitters, etc. loaded once per run, updated in-place, committed back to the repo at the end of the workflow.

the hit streak section is keyed by MLB player ID:

```json
{
  "hitting_streaks": {
    "666139": { "name": "Josh Lowe",      "team": "Angels",  "streak": 0, "last_date": "2026-04-29" },
    "666152": { "name": "David Hamilton", "team": "Brewers", "streak": 6, "last_date": "2026-04-30" },
    "666160": { "name": "Mickey Moniak",  "team": "Rockies", "streak": 9, "last_date": "2026-04-30" }
  }
}
```

`streak` is the current consecutive-game hit streak. `0` means the streak is broken. a streak only resets when a player appears in a box score with at-bats but zero hits — players who don't appear at all (rest day, not in the lineup) are left untouched, which is correct. the year in the filename means it auto-resets every spring.

a rare event is surfaced when a streak crosses a milestone threshold: **20, 30, or 40 games**. streaks below 20 are tracked silently.

**tier 3 — historical retrosheet corpus**

a parquet file (`retrosheet-patterns.parquet`) built from [retrosheet](https://www.retrosheet.org/) event data going back to 1918. for each pattern extracted from today's box scores, the rarity engine queries the corpus for prior occurrences by the same team and computes the gap. if the gap exceeds 5 years (~1825 days) it surfaces a "first time in X years" event.

currently handles 6 patterns: `stolen_base_burst`, `back_to_back_hr_inning`, `three_hr_inning`, `walk_off_grand_slam`, `immaculate_inning`, and `position_player_pitching`.

see [docs/rarity-engine.md](docs/rarity-engine.md) for the full details on how the corpus is built and queried.

## corpus setup (tier 3)

the parquet file is already committed to the repo, so you don't need to rebuild it just to run the bot. if you want to regenerate it (e.g. to add a new season), there are two ways:

**option 1: github actions (easiest)**

go to the actions tab → "Update Retrosheet Corpus" → "Run workflow." you can specify a start and end year. the workflow installs chadwick, runs `corpus.py`, and commits the new parquet back to the repo.

**option 2: locally**

requires the chadwick tools:

```bash
brew install chadwick   # macOS
# debian/ubuntu: chadwick is not in the apt repos — download a prebuilt binary from
# https://github.com/chadwickbureau/chadwick/releases and put cwevent on your PATH
```

then run the backfill (takes an hour or so — downloads from the Chadwick Bureau's GitHub mirror, parses with `cwevent`):

```bash
# full backfill 1918-2024
python highlights/corpus.py

# or a specific year range
python highlights/corpus.py 2000 2024
```

the parquet is written to `retrosheet-patterns.parquet` in the repo root. commit it when you're happy with it.

## cron timing

`0 13 * * *` UTC = 9:00 AM ET.

west coast games routinely end around 1 AM ET. running at 9 AM gives the MLB ~8 hours to finalize box scores before we pull them. running earlier risks grabbing incomplete data for late games — especially if it went to extras and the manfred runner isn't helping close things out.

## config reference

| variable | where it lives | what it does |
|----------|---------------|-------------|
| `SLACK_WEBHOOK_URL` | github secret | incoming webhook URL for posting highlights; only required if `POST_TO_SLACK=true` |
| `ANTHROPIC_API_KEY` | github secret (optional) | anthropic API key for claude narrative |
| `AI_SUMMARY` | workflow env var | set to `"true"` to enable the claude summary; default `"false"` |
| `POST_TO_SLACK` | workflow env var | set to `"false"` to skip the slack post; recap file is always written regardless; default `"true"` |
