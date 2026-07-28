# rarity engine

the rarity engine (`highlights/rarity.py` + `highlights/corpus.py`) is what makes the service interesting.
it's fully deterministic, and finds historically notable events from each day's box scores so the downstream AI call only has to write prose — it never invents facts or "first since" claims on its own.

entry point: `find_rare_events(box_scores, target_date)` — returns a list of event dicts. each dict has `kind`, `label`, `description`, and `since`. callers don't need to know which tier produced a given event.

## the three tiers

**tier 1 — single-game box score checks.**
calculated from the box score `get_scores.py` already pulled.
always available, and fully deterministic.
catches facts for the day:

* hit for the cycle (`_check_cycle`)
* no-hitters and perfect games (`_check_no_hitter`)
* immaculate inning — 3 Ks on exactly 9 pitches (`_check_immaculate_inning`)
* 3+ home runs by one player in a game (`_check_multi_hr_individual`, threshold=3)
* position player pitching (`_check_position_player_pitching`)
* 10+ strikeout games (`_check_double_digit_k`)
* shutouts (`_check_shutout`)

tier 1 events set `since: None` — they don't know how long it's been since the last one. that context comes from tier 3 if it's available.

**tier 2 — season ledger.**

a small JSON file (`data/season-ledger-{year}.json`) populated and updated across daily runs.

it tracks:

* hitting streaks: every batter's current consecutive-game streak. surfaces events at milestones 20, 30, 40.
* no-hitter teams: which teams have already thrown one this season (to detect season firsts).
* season firsts: date of the first cycle, first no-hitter, etc.

read once at the start of each run, checked against every box score for the day, and written back at the end. the year in the filename keeps it clean between seasons. the workflow commits the updated file back to the repo after each run.

**tier 3 — historical data.**
a parquet file (`retrosheet-patterns.parquet`) in the repo root, built from post-dead-ball-era retrosheet events.
for each pattern extracted from today's box scores, the script queries the corpus for prior occurrences by the same team and computes the gap. if the gap exceeds 5 years (~1825 days) it surfaces a "first time in X years" event.

tier 3 currently handles 6 patterns: `stolen_base_burst`, `back_to_back_hr_inning`, `three_hr_inning`, `walk_off_grand_slam`, `immaculate_inning`, and `position_player_pitching`.

## why parquet

the corpus is an immutable historical record — once a retrosheet year is processed it doesn't change. it's also heavily columnar: the interesting queries filter by `team` and `pattern` then aggregate on `date`. parquet's layout and filtering make those queries nearly instant even on millions of rows.

alternatives considered:

* **sqlite** — would work fine but means managing a database file, connections, and schema migrations. parquet is just a file.
* **csv** — same query performance as a full table scan every time. parquet's column-based reads are way faster for "give me all rows where team='NYY' and pattern='walk_off_grand_slam'."
* **postgres / cloud sql** — needs a server, connection pooling, VPC config. the corpus has no writes during the daily run (it's read-only in `rarity.py`). a serverless file is simpler.
* **bigquery** — way overkill. the retrosheet corpus is a few hundred megabytes, the queries are simple, and you'd be adding IAM and another cloud service dependency just to query a file.

pandas reads parquet directly with `pd.read_parquet('retrosheet-patterns.parquet')`.

## retrosheet data source

retrosheet (retrosheet.org) is a database of play-by-play data for MLB games going back to 1871. we only use the post-dead-ball era (1918+).

`corpus.py` pulls from the Chadwick Bureau's GitHub mirror:

```
https://github.com/chadwickbureau/retrosheet/tree/master/seasons/{year}/
```

each year directory contains event files (`.EVA` for American League, `.EVN` for National League, `.EVE` for other), roster files (`.ROS`), and a `TEAM` file. the files are in a custom text format that needs the Chadwick tools to parse.

**parsing with cwevent.** `cwevent` (from [the Chadwick tools](https://chadwick.readthedocs.io/en/latest/)) converts retrosheet event files into CSVs with one row per plate appearance. `corpus.py` runs it with `-n` (named headers), `-f` (specific field subset), and `-x 19` (extended field: pitcher-is-starter flag). the script processes one file at a time.

**key cwevent columns** (after `_normalize_columns` renames them):

| column | meaning |
|--------|---------|
| `GAME_ID` | e.g. `ANA200304080` — home team (3 chars) + YYYYMMDD + game number |
| `BAT_EVENT_FL` | `T` = true batting event; filter to this to drop baserunning rows |
| `EVENT_CD` | numeric event type: e.g. `23` = home run, `3` = strikeout |
| `INN_CT` | inning number, 1-indexed |
| `BAT_HOME_ID` | `1` = home team batting, `0` = away team batting |
| `PITCH_SEQ_TX` | pitch-by-pitch sequence: `S`=swinging K, `C`=called K, `K`=final-pitch K, `T`=foul-tip K, `B`=ball, `F`=foul, etc. |
| `BASE1/2/3_RUN_ID` | runner IDs on each base (empty string = no runner) |
| `HOME_SCORE_CT` / `AWAY_SCORE_CT` | cumulative scores after each play |
| `PIT_START_FL` | `T` if the pitcher started the game (extended field 19) |

`HOME_TEAM_ID` is not a direct column — it's derived from the first 3 characters of `GAME_ID`. `AWAY_TEAM_ID` comes from the `VISITING_TEAM` column (renamed in `_normalize_columns`).

**patterns the corpus detects:**

* `back_to_back_hr_inning` — two consecutive batters both hit HRs in the same half-inning
* `three_hr_inning` — a team hits 3+ HRs in one half-inning (not necessarily consecutive)
* `walk_off_grand_slam` — bases-loaded HR in the final home half-inning, winning a game the team trailed by 1–4 runs
* `immaculate_inning` — 3 Ks on exactly 9 pitches; detected by inspecting `PITCH_SEQ_TX` and filtering out non-pitch annotation chars (`>`, `+`, `*`, digits, `N`, `V`)
* `position_player_pitching` — a non-pitcher appearing on the mound; uses a row-ordering heuristic (batted before pitching) rather than set intersection to avoid massive false positives from NL relief pitchers pre-2022 universal DH

## previewing the corpus locally

the parquet file is at `retrosheet-patterns.parquet` in the repo root. works with standard tools:

```bash
# TUI viewer
brew install parqeye
parqeye retrosheet-patterns.parquet

# quick python inspection
python3 -c "import pandas as pd; print(pd.read_parquet('retrosheet-patterns.parquet').head())"

# sql queries with duckdb
duckdb retrosheet-patterns.parquet
```

in duckdb:
```sql
-- all walk-off grand slams since 2000
SELECT * FROM 'retrosheet-patterns.parquet'
WHERE pattern = 'walk_off_grand_slam' AND date > '2000-01-01'
ORDER BY date DESC;

-- immaculate innings by team
SELECT team, COUNT(*) AS n FROM 'retrosheet-patterns.parquet'
WHERE pattern = 'immaculate_inning'
GROUP BY team ORDER BY n DESC;
```

## one-time backfill

the parquet is already in the repo, so you only need to run this if you want to rebuild it (e.g. to extend the year range or add a new pattern).

**option 1: github actions**

go to the actions tab → "Update Retrosheet Corpus" → "Run workflow." provide start and end years. the workflow installs chadwick, runs `corpus.py`, and commits the result back to the repo.

**option 2: locally**

requires the chadwick tools:
```bash
brew install chadwick   # macOS
# debian/ubuntu: chadwick is not in the apt repos — download a prebuilt binary from
# https://github.com/chadwickbureau/chadwick/releases and put cwevent on your PATH
```

run the full backfill (1918–2024, takes an hour or so as it downloads from the Chadwick Bureau's GitHub repo, parses with `cwevent`):
```bash
python highlights/corpus.py
```

or a specific year range:
```bash
python highlights/corpus.py 2000 2024
```

the parquet is written to `retrosheet-patterns.parquet` in the repo root. inspect it with duckdb or pandas, then commit.

## incremental updates

`update_corpus_for_year(year)` re-scans the entire current year and replaces those rows in the parquet. it doesn't diff — retrosheet event files for in-progress seasons are updated throughout the year, so the simplest correct approach is a full year replace.

to add the current season:

```python
from highlights.corpus import update_corpus_for_year
update_corpus_for_year(2025)
```

then commit the updated `retrosheet-patterns.parquet`.

## known limitations and edge cases

**combined no-hitters.** `_check_no_hitter` in tier 1 only catches individual complete-game no-hitters. combined no-hitters (where two or three pitchers share the work) require aggregating IP + H across pitchers — each pitcher's individual line will show less than 9 IP, so none of them trigger the single-pitcher check. the event might surface through tier 3 retrosheet matching eventually, but it's not explicitly detected.

worth repeating: the tier 3 corpus has no `combined_no_hitter` pattern. combined no-hitters won't surface through corpus matching — only through tier 1 if a single pitcher completes the game without hits.

**retrosheet data coverage.** pre-1950 years have incomplete or missing `PITCH_SEQ_TX` data. immaculate inning detection silently returns nothing for those years (guards for missing `PITCH_SEQ_TX` column). `BAT_EVENT_FL` and some score columns can also be absent in very old files — `_extract_patterns_from_events` checks for required columns and skips the year entirely if any are missing.

**position player pitching false positives.** the row-ordering heuristic (batted before their first pitch appearance) eliminates the bulk of false positives from NL relief pitchers pre-2022, but it can still miss early-game position-player appearances and will misclassify the rare pitcher used as a pinch-hitter in inning N who then pitches in inning N+1. the code comments acknowledge this as an acceptable edge case.

**walk-off grand slam approximation.** the `home_before` computation subtracts 4 from the home score after the hit (assuming a grand slam always scores exactly 4 runs). this is correct for a classic walk-off grand slam but would misclassify a walk-off inside-the-park grand slam where the batter scores on an error or only 3 runners actually score — retrosheet `EVENT_CD=23` includes inside-the-park HRs.

**team-code vocabularies (StatsAPI vs retrosheet).** the corpus keys teams by retrosheet franchise code (`NYA`, `CHN`, `LAN`, ...), derived from `GAME_ID`, while live box scores key teams by MLB StatsAPI abbreviation (`NYY`, `CHC`, `LAD`, ...). these are different namespaces, so a raw join misses ~1/3 of franchises (Yankees, Dodgers, Cubs, Mets, Cardinals, Giants, etc. — 33% of corpus rows). `_STATSAPI_TO_RETROSHEET` / `_to_retrosheet_code` in `rarity.py` translate the live abbrev to the current-franchise retrosheet code before the join. only current franchises are mapped; pre-relocation eras (Montreal `MON`, California `CAL`, ...) are intentionally out of scope. note that a mapping must resolve to the code the corpus uses for the *recent* era — the Marlins are keyed `MIA` for 2012-present (identical to the StatsAPI abbrev) and only `FLO` for 1993–2011, so `MIA` is left as an identity passthrough; remapping it to `FLO` would strand every Marlins game in the pre-2012 history.

**per-pattern gap thresholds.** a single flat 1825-day (5-year) cutoff suppressed almost every pattern, because common events recur far more often than that for any one team — a club hits back-to-back HRs every ~3 weeks in season, so those gaps never clear 5 years. `_PATTERN_MIN_GAP_DAYS` in `rarity.py` now sets a per-pattern fuse (rarer events keep a long fuse, common ones a shorter one), falling back to `_DEFAULT_MIN_GAP_DAYS` (5 years). the comparison is `>=` so an exactly-on-the-threshold gap still surfaces.

**stolen_base_burst has no corpus detector (known no-op).** `_extract_patterns` emits a `stolen_base_burst` pattern, but `corpus.py` has no matching detector, so the parquet contains zero `stolen_base_burst` rows. its tier 3 lookup is therefore always empty and it never surfaces. it's left in place intentionally; making it functional requires adding a detector to `corpus.py` and rebuilding the parquet.

**season ledger concurrent writes.** `_write_season_ledger` does a full overwrite each run. concurrent invocations (e.g. if the workflow fires twice somehow) could stomp each other. in practice the function runs once per day so this is theoretical, but it's worth knowing.

**team name extraction in tier 2.** the season-first no-hitter check extracts the team name from the tier 1 event's `description` string (the parenthesized portion after the pitcher name) rather than from structured data. this is fragile if the description format ever changes.
