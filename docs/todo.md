# To do

What's left to build, plus findings from the code review of 2026-10-20.
Tick items off as they're done; record any decisions they need in
[decisions.md](decisions.md).

**Where we left off (2026-10-20):** steps 1–3 are done and pushed: the
collector runs locally with `uv run db-ml-collector`. **Next is step 4:
deploying to the Hetzner VPS**, continued from a laptop that can reach the
VPS. See the step 4 section below for the full plan.

## Done 2026-10-20 (from the code review)

- [x] **`clean_stations.py` in line with CLAUDE.md:** docstrings, and it uses
  the shared `files.py` helpers, so it reads and writes UTF-8 and the output
  keeps umlauts readable. Output data unchanged.
- [x] **Leverkusen added** by name (D12). It ranks #49 and Heilbronn drops
  out. Its one EVA had 16 trains in the two sample hours, none of them S-Bahn.
- [x] **One shared API client with retries** (`api.py`): retries 429, 5xx
  and network errors with doubling backoff, and follows `Retry-After`. Used
  for StaDa, Timetables and Wikidata.
- [x] **Shared JSON helpers** (`files.py`), replacing the four variants.
- [x] **Reloading switches off stale EVAs** (`db.sync_station_evas`). Seen
  working: Heilbronn's EVA went to `collect = 0` when it left the top 60.
- [x] **Dropped `evaNumber` from `top_stations.json`.** Coordinates now fall
  back to any EVA entry that has them, instead of crashing.
- [x] `station_evas.py` samples two hours (08:00 and 17:00). No collect
  decision changed compared with the one-hour run.
- [x] Name fallback in `city_populations.py` only matches within the
  station's federal state (first two digits of the municipality key).
- [x] `city_populations.py` handles `null` mailing address or city.
- [x] `stations.py` raises an error if a response has no `total`.

## Next steps

### Step 2: Turn Timetables XML into rows (done 2026-10-20, see D13)
- [x] Tried `/rchg` against the real API: works; each entry is the stop's
  full current state, same as `/fchg`.
- [x] Parse `/plan`, `/fchg` and `/rchg` (`timetable_xml.py`) and store them
  (`db.save_plan`, `db.save_changes`). Tested against real Frankfurt XML in
  `tests/fixtures/`.
- [x] Times converted to ISO 8601 with UTC offset; the repeated hour on
  25 Oct 2026 is tested.
- [x] Changes for unknown stops: created if the entry has full train details,
  otherwise skipped until the `/plan` row exists.
- [x] Cancellations, reinstated trains, coupled trains (`coupled_with`).
- [x] S-Bahn trains skipped.
- [x] `stop_changes` row only when the state differs from the last one
  (built here rather than in step 3).
- [x] Live run on Frankfurt, Berlin and München: 292 stops, 282 snapshots,
  696 messages; a follow-up `/rchg` added only the stops that changed.

### Step 3: Collector loop (done 2026-10-20, see D15)
- [x] Polling schedule (`collector.py`): `/rchg` every 90 s per EVA (spread
  out), `/plan` for the next 4 hours as they come into view, `/fchg` hourly.
- [x] At startup, `/plan` for the past 12 hours too, in the background.
- [x] One rate limiter on the HTTP client, so retries count too.
- [x] Errors are logged and the loop carries on; requests retry once,
  briefly; failed `/plan` fetches are retried every 5 minutes while their
  hour is in the window. A summary is logged every 5 minutes.
- [x] Restart recovery: startup backfill plus `/fchg` catches up.
- [x] Clean stop on SIGTERM/SIGINT. Run with `uv run db-ml-collector`.

- [ ] Check real data for messages flagged as deleted (`del` attribute, not
  seen in 700+ sampled stops). If they occur, store the flag and update the
  message instead of `INSERT OR IGNORE` (code review, 2026-10-20).

### Step 4: Deploy to the Hetzner VPS (next)

**Not in git, so not on a fresh clone:**
- `.env` (API credentials: `DB_CLIENT_ID`, `DB_API_KEY`,
  `DB_STATIONS_ENDPOINT`, `DB_TIMETABLES_ENDPOINT`). Copy it to the VPS by
  hand (e.g. `scp`), never through git, and `chmod 600` it.
- `data/` (station files and the database). Don't copy the old laptop's
  `data/db_ml.sqlite`: it only holds test runs from 2026-10-20. Rebuild the
  reference data on the VPS instead (below).

**Before the first real collection run:**
- [ ] **Schema migrations.** Once real data exists, the database can't just
  be recreated (as it was during steps 1–3). Add simple numbered migrations,
  e.g. tracked with SQLite's `PRAGMA user_version`, with the current schema
  as version 1. Do this first, so the VPS database starts on version 1.

**On the VPS:**
- [ ] Install uv, clone the repo, `uv sync` (uv installs Python 3.13 from
  `.python-version` if needed).
- [ ] Copy `.env` over. Pick a database location outside the repo, e.g.
  `/var/lib/db-ml/db_ml.sqlite`, and set `DB_ML_DATABASE_PATH` to it.
- [ ] Build the reference data. Takes about 5 minutes; the last two steps
  call the Timetables API:
  ```
  uv run python -m db_ml.stations
  uv run python -m db_ml.clean_stations
  uv run python -m db_ml.city_populations
  uv run python -m db_ml.top_stations
  uv run python -m db_ml.station_evas
  uv run python -m db_ml.db
  ```
  Expect 61 stations and 63 EVA numbers to collect.
- [ ] systemd service: `ExecStart` running `uv run db-ml-collector` (use
  uv's absolute path), `WorkingDirectory` = the repo, `EnvironmentFile` =
  the `.env`, `Restart=always`, `RestartSec=30`, a dedicated user. It stops
  cleanly on SIGTERM within about a second.
- [ ] Check the logs (`journalctl -u <service>`): a summary line every
  5 minutes. After a (re)start, the plan backfill takes about 70 minutes
  and the first `/fchg` waits for it (D15). That's expected.
- [ ] Nightly backup off the VPS (Hetzner Storage Box or a local machine).
  Use SQLite's online backup (`sqlite3 <db> ".backup <file>"`), which is
  safe while the collector writes; copying the file directly isn't, in WAL
  mode.

**After it has run for a while:**
- [ ] After day one, measure database growth against the ~10 GB/year
  estimate (D11) and check free disk space. The 5-minute live run stored
  ~700 snapshots, but that was inflated by first-time snapshots.
- [ ] **Clock change, night of 24/25 Oct 2026:** check that night's data
  against the rules in D14 (forecasts in the repeated 02:00 hour,
  departures after arrivals, one `/plan` fetch for local 02:00).
- [ ] The deleted-message (`del`) check from step 3's list, on real data.

### Later
- [ ] Journey builder: derive `journeys` from `stops` (D7).
- [ ] Find DB's list of message codes (e.g. code 55 on ICE 372).
- [ ] Delay prediction model.
- [ ] Prediction API.
- [ ] React frontend.
- [ ] Add the pipeline commands to CLAUDE.md.
