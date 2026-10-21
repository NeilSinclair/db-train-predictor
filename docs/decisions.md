# Decision log

Every project decision goes here, newest at the bottom. Each entry says what
we decided, why, and what follows from it. If a decision is reversed, add a
new entry that supersedes the old one rather than editing history.

Statuses: **Accepted** (decided), **Open** (being discussed, not decided yet),
**Superseded** (replaced by a later entry).

---

## D1. Project conventions
- **Date:** 2026-10-20
- **Status:** Accepted
- **Decision:** Python is managed with uv. All code has tests. Python uses
  Google-style docstrings. The frontend is React.
- **Details:** See [CLAUDE.md](../CLAUDE.md).

## D2. Main stations are the stations named "Hbf" or "Hauptbahnhof"
- **Date:** 2026-10-20
- **Status:** Accepted
- **Context:** We pull all stations from the StaDa API (`station-data/v2`)
  and need a manageable set to start with.
- **Decision:** A station is a main station if its name contains `Hbf` or
  `Hauptbahnhof`. The second check exists because StaDa spells Berlin's
  station out in full ("Berlin Hauptbahnhof"); it is the only such station.
- **Consequences:** 134 main stations (`clean_stations.py`).

## D3. City populations come from Wikidata, matched on the municipality key
- **Date:** 2026-10-20
- **Status:** Accepted
- **Context:** We want to rank stations by how big their city is.
- **Decision:** Look up populations on Wikidata (free, no API key). Match on
  the official German municipality key (Amtlicher Gemeindeschlüssel), which
  StaDa provides as `municipalityCode`. Fall back to the city name when the
  key doesn't match. Where Wikidata has several figures, use the most recent.
- **Why:** Matching on the key avoids name mismatches such as "Oldenburg (Oldb)"
  vs "Oldenburg". The name fallback covers keys that changed: Hanau got a new
  key when it became a district-free city on 2026-01-01, and StaDa still has
  the old one.
- **Consequences:** All 133 municipalities matched (`city_populations.py`).

## D4. Start with the main stations of the 60 largest cities
- **Date:** 2026-10-20
- **Status:** Accepted
- **Decision:** Keep the 60 most populous cities. A city with more than one
  main station keeps all of them, so Mönchengladbach contributes both
  Mönchengladbach Hbf and Rheydt Hbf, giving 61 stations. For each station
  we keep only: StaDa station number, EVA number, name, city, population,
  latitude and longitude.
- **Consequences:** `data/processed/top_stations.json` (`top_stations.py`).

## D5. Use JSON files for now, move to SQLite for timetable data
- **Date:** 2026-10-20
- **Status:** Accepted
- **Decision:** Station and city reference data stays in JSON and is joined
  in Python. When we start collecting timetable and delay data, we move to
  SQLite.
- **Why:** The reference data is ~130 rows, so a database adds setup without
  saving work. Timetable data will grow continuously and needs querying by
  station, train and time.

## D6. Verify each station's EVA number against the Timetables API
- **Date:** 2026-10-20
- **Status:** Accepted (follow-up open, see below)
- **Context:** StaDa marks one EVA number per station as `isMain`. For Berlin,
  the Timetables API returns no trains for that number.
- **Decision:** Don't trust StaDa's `isMain` EVA blindly. Check every station's
  EVA numbers against the Timetables `/plan` endpoint.
- **Findings** (checked 2026-10-20, 18:00 hour, all 72 EVA numbers of the 61 stations):
  - Berlin's main EVA (`8011160`) has no trains. Main-line trains are on
    `8098160` ("Berlin Hbf"), S-Bahn on `8089021`. The Timetables
    `/station` lookup also returns `8098160` for Berlin Hbf.
  - Hamburg, München, Frankfurt, Leipzig, Stuttgart and Potsdam have trains on
    their main EVA, plus extra EVA numbers for S-Bahn or underground
    (*tief*) platforms. München also splits two regional platform groups
    (Gl. 27–36, Gl. 5–10) onto separate EVA numbers.
  - Leipzig's `8089318` is not a valid Timetables EVA (HTTP 400).
  - Every other station works with its main EVA.
- **Follow-up:** Resolved by D8 (no S-Bahn). `top_stations.py` still needs
  updating to store the EVA number(s) that actually have trains.

## D7. How to build train journeys from station data
- **Date:** 2026-10-20
- **Status:** Accepted
- **Context:** The Timetables API is per station. To know where a train
  started and how its delay develops along the route, we need to link a
  train's stops across stations.
- **Finding:** Each stop's `id` has the form
  `<trip id>-<YYMMDDHHmm of first departure>-<stop index>`. The first two
  parts are the same at every station on the trip. Verified with ICE 372 on
  2026-10-20: Erfurt (stop 16) → Halle (17) → Berlin (18).
- **To decide:** table design, what to store per stop, how often to poll,
  and how to handle stops at stations we don't collect.
- **Proposal:** six SQLite tables (`stations`, `station_evas`, `stops`,
  `stop_changes`, `stop_messages`, and `journeys` derived later), drawn with
  real example rows at https://claude.ai/artifact/2dpuXf5Ft8pbywym3A2CCw.
- **Decision:** Accept the proposal, including its four suggestions:
  one platform column (split later only if arrival and departure differ);
  collect `stop_messages` from the start; store times with their UTC offset
  (e.g. `2026-10-20T17:18+02:00`); coupled trains are two journeys linked to
  each other.

## D8. Don't collect S-Bahn for now
- **Date:** 2026-10-20
- **Status:** Accepted
- **Context:** S-Bahn trains have their own EVA numbers at some stations
  (see D6), and a few also appear on main EVAs.
- **Decision:** Collect main-line trains only. Skip S-Bahn EVA numbers, and
  skip trains with category `S` even on EVAs we do collect.
- **Why:** With one station per city, we'd see almost none of an S-Bahn
  line's stops (in Berlin, only Hauptbahnhof). S-Bahn delays are interesting
  but need many more stations per city; we may add them later.
- **Consequences:** We collect 63 EVA numbers: the main-line EVA of each of
  the 61 stations (Berlin: `8098160`), plus München's two regional platform
  groups (`8098261` Gl.27–36, `8098262` Gl.5–10), which carry RE/RB trains.
- **Rule used in code** (`station_evas.py`): an EVA is skipped if it has no
  trains or at least 90% of its trains are S-Bahn. A simple majority rule
  was tried first and was wrong: Bonn, Dresden, Essen, Heidelberg,
  Ludwigshafen and Rostock list S-Bahn and main-line trains under the same
  EVA, so it dropped those stations entirely.

## D9. Keep the full history of updates to each stop
- **Date:** 2026-10-20
- **Status:** Accepted
- **Decision:** Store every change we observe for a stop (forecast times,
  platform, cancellation) with the time we saw it, not just the latest value.
- **Why:** The model has to predict from what is known at a given moment,
  such as 30 minutes before departure. Training only on final values would
  leak the answer.

## D10. Collector hosting
- **Date:** 2026-10-20
- **Status:** Accepted
- **Constraints:** The collector must run 24/7, because delays are only
  visible live. The Timetables API allows 60 requests per minute. 63 EVA
  numbers fit at roughly 44 requests a minute (`/rchg` every 90 s per EVA,
  `/plan` and `/fchg` hourly); `/rchg` has not been tested yet.
- **Options considered:** Google Cloud e2-micro, Oracle Cloud Always Free,
  Cloudflare Workers + D1 (100k rows written/day cap), GitHub Actions
  (5-minute minimum, no storage).
- **Decision:** Run the collector on our existing Hetzner VPS.

## D11. SQLite on the VPS's existing disk
- **Date:** 2026-10-20
- **Status:** Accepted
- **Context:** No Postgres is running on the VPS. The VPS has a 40 GB disk.
- **Decision:** Use SQLite (WAL mode) on the VPS's existing disk, with no
  separate volume. The database path is configurable, so it can move to a
  volume later by copying the file. Back up nightly to somewhere off the VPS.
- **Why:** The collector is the only writer and runs on the same machine as
  the database, which is the case SQLite handles best. Nothing to install or
  run. Rough estimate is ~10 GB/year including indexes; to be checked against
  real growth after the first day of collecting.
- **Consequences:** Keep all SQL in one module so a later move to Postgres
  stays contained.

## D12. Add Leverkusen Mitte as a main station
- **Date:** 2026-10-20
- **Status:** Accepted
- **Context:** The code review found that Leverkusen (168k people) was missing
  from the top 60: its main station is "Leverkusen Mitte", so D2's
  `Hbf`/`Hauptbahnhof` name filter skips it. Checked against Wikidata's list
  of cities over 100k, it was the only city above the cutoff we missed.
- **Decision:** Add "Leverkusen Mitte" to the main stations by name
  (`EXTRA_MAIN_STATIONS` in `clean_stations.py`).
- **Consequences:** Leverkusen ranks #49. Heilbronn (#60 before) drops out;
  its EVA stays in the database with `collect = 0`. Still 61 stations and 63
  EVA numbers to collect.

## D13. How Timetables data is parsed and stored
- **Date:** 2026-10-20
- **Status:** Accepted
- **Context:** Building step 2 (XML → rows), checked against live responses
  for Frankfurt(Main)Hbf on 2026-10-20.
- **Findings:**
  - Each `/rchg` entry is the stop's full current change state, identical
    to its `/fchg` entry (25 of 25 matched). Both feeds are parsed and
    stored the same way. Calls 30 s apart overlapped on 24 of 25 stops, so
    polling every ~90 s inside the 2-minute window is safe.
  - `/fchg` reaches back roughly 8–12 hours and a few hours ahead.
  - Message types: `d` delay reason (numeric code), `q` quality, `f` free
    text, `h` disruption notice with a category such as `BAUARBEITEN` and
    no code.
  - `cs="c"` means cancelled; `cs="p"` means reinstated after a cancellation.
- **Decisions:**
  - **Clock change:** times are resolved to Europe/Berlin with the right UTC
    offset. A local time that happens twice (02:00–02:59 when clocks go
    back) is resolved to whichever occurrence is closer to a reference: the
    planned time for forecasts, otherwise the fetch time.
  - **Changes for unknown stops:** if the change-feed entry names the train
    (`tl`), the stop is created from it (e.g. trains added at short notice).
    Otherwise it's skipped; `/fchg` reports it again once its `/plan` hour
    has been fetched. Nothing is buffered.
  - **Schema addition:** `stop_messages` gets a `category` column, so
    disruption categories such as construction work are kept as a possible
    model feature.
- **Consequences:** The collector (step 3) should fetch `/plan` at least 4
  hours ahead, and at startup also the past hours of today, so that
  `/fchg` changes have a stop to attach to. There is no migration tool yet;
  schema changes before deployment mean recreating the local database.

## D14. Store times in UTC; rules for the repeated hour
- **Date:** 2026-10-20
- **Status:** Accepted. Supersedes the time format in D7
  (`2026-10-20T17:18+02:00`) and the clock-change rule in D13.
- **Context:** The code review of step 2 found that ISO text with mixed
  offsets (`+02:00` in summer, `+01:00` in winter) sorts in the wrong order
  across the clock change, which breaks `ORDER BY` and range queries.
- **Decision:** Store every time as ISO 8601 text in UTC
  (e.g. `2026-10-20T15:18:00+00:00`). Convert to Europe/Berlin when reading.
  When a local time happens twice (02:00–02:59 when clocks go back):
  - a forecast is never read as more than 10 minutes early; among the
    remaining options, the one closest to the planned time wins (the
    smaller delay);
  - a planned departure is never before the stop's planned arrival;
  - the trip start always takes the first occurrence, so every station
    gets the same (trip ID, trip start) and journeys aren't split;
  - a time that doesn't exist (02:00–02:59 when clocks go forward) is
    moved forward an hour.
- **Known limitation:** a planned time in the repeated hour with nothing
  earlier at that stop to anchor it is resolved against the fetch time. A
  trip that really started in the second 02:00 hour gets a start an hour
  early. Both affect a handful of night trains once a year.

## D15. Collector schedule and behaviour
- **Date:** 2026-10-20
- **Status:** Accepted
- **Decision:** One loop (`collector.py`) with three task types, run in this
  priority order when several are due:
  1. `/rchg` for each EVA every 90 s, spread evenly across the interval;
  2. `/plan` for each hour from 12 hours back to 4 hours ahead, fetched
     once per (EVA, hour), new hours added as they come into view;
  3. `/fchg` for each EVA every hour, as a catch-up.
  A rate limiter on the HTTP client spaces every request, retries
  included, at least 1.05 s apart. A failed task is logged and the loop
  continues. If a whole polling interval is missed (e.g. the machine was
  suspended), the task runs once straight away and resumes its rhythm from
  there. SIGTERM/SIGINT stop the loop within about a second.
- **Revised after the code review (same day, before the first commit):**
  - Requests retry once, after 1 s, waiting at most 5 s even if the API
    asks for longer, with a 10 s timeout. `/rchg` has only ~30 s of slack,
    so one slow station must not stall everyone else's polls.
  - A failed `/plan` is retried every 5 minutes for as long as its hour is
    within the 12-hours-back window, instead of giving up after 3 tries
    and losing the hour.
  - The plan window always covers 12 hours back, not only at startup, so
    hours missed during a long pause are fetched.
  - Plan hours are keyed by German local hour, so the repeated 02:00 hour
    on the clock-change night is fetched once (the API can't tell the two
    apart either).
  - The collector refuses to start if `/rchg` would need more than 80% of
    the request budget (86+ EVAs), since `/plan` and `/fchg` would starve.
  - Shutdown polls a stop flag once a second instead of waiting on it,
    which avoids a possible deadlock in the signal handler.
- **Why:** `/rchg` must never fall behind or changes are lost; the plan
  backfill and `/fchg` can wait. Restarting needs no extra recovery logic:
  the startup backfill and the first `/fchg` fill the gap.
- **Measured** (live run, 63 EVAs, 5.5 minutes, 2026-10-20 20:21):
  316 requests at 57.3/min, busiest 60-second window 58, all HTTP 200;
  response time median 0.04 s, max 0.15 s; 231 `/rchg` polls at most 1 s
  late. The startup backfill runs at ~15 plan fetches a minute alongside
  `/rchg`, so it takes ~70 minutes, and the first `/fchg` waits for it.

## D16. Run the collector in Docker Compose on the VPS
- **Date:** 2026-10-20
- **Status:** Accepted. Replaces the systemd service planned in todo.md's
  step 4 (that plan was never a numbered decision).
- **Context:** The VPS (Ubuntu 24.04, ARM64, 4 GB) already runs other
  services (`mybot`, `openclaw-gateway`). It had no Docker and no uv.
- **Decision:** One `collector` service in `compose.yaml`, built from the
  repo's `Dockerfile` (`python:3.13-slim` + uv, locked dependencies) on the
  VPS itself, so it's native ARM64. `restart: unless-stopped`; credentials
  through `env_file: .env`; logs rotated at 5 × 10 MB. Code lives in the
  image; reference data and the database live in `/var/lib/db-ml`, mounted
  at `/app/data`, so the relative `data/...` paths keep working. The
  container runs as UID/GID 10001, which owns the data folder.
- **Why:** Keeps the collector's Python and dependencies separate from
  what's already on the VPS, and makes a redeploy one command
  (`docker compose up -d --build`). The image has the time zone data that
  the clock-change rules (D14) rely on (checked).
- **Consequences:** Runbook in [deployment.md](deployment.md). The
  reference-data pipeline runs in the same image
  (`docker compose run --rm collector ...`).

## D17. Schema migrations with `PRAGMA user_version`
- **Date:** 2026-10-20
- **Status:** Accepted
- **Context:** Once real data is collected, the database can't be recreated
  for a schema change, as it was during steps 1–3 (D13).
- **Decision:** `db.MIGRATIONS` is an ordered list of SQL scripts; the
  database's `user_version` says how many have run. `init_db` applies the
  pending ones at startup, each in one transaction with its version bump,
  and refuses to open a database newer than the code. The schema as of the
  first deployment is version 1. Migrations are only ever appended, never
  edited.
- **Consequences:** The VPS database started on version 1. A database from
  before migrations (version 0) becomes version 1 without changes, since
  version 1 only uses `CREATE ... IF NOT EXISTS`.

## D18. Remember fetched `/plan` hours in the database
- **Date:** 2026-10-20
- **Status:** Accepted. Changes D15's restart behaviour.
- **Context:** The collector kept the list of fetched plan hours in memory
  only, so every restart fetched all 17 hours × 63 EVAs (~1,070 requests,
  ~70 minutes) again, and the `/fchg` catch-up waited behind them. We expect
  many restarts while building the project; restarting more often than
  every 70 minutes would have kept `/fchg` from ever running.
- **Decision:** New table `plan_fetches (eva, hour_label, fetched_at)`,
  added by migration 2 (D17). A plan hour is recorded once its stops are
  saved, or when the API rejects it (400/404, which a retry won't change).
  Failed fetches aren't recorded, so they're retried as before. At startup
  the collector skips hours already recorded.
- **Consequences:** A short restart fetches only the hours it missed, and
  `/fchg` catches up within a minute or two. A restart now costs the few
  seconds of downtime, which `/rchg`'s 2-minute window covers. The table
  grows by ~550k small rows a year. The first start after this change still
  fetches everything once, since earlier fetches weren't recorded.

## D19. Model training in its own package and container on the VPS
- **Date:** 2026-10-20
- **Status:** Accepted (no training code yet)
- **Context:** The model will be trained on the VPS; the frontend will be
  hosted elsewhere. The VPS has 2 vCPUs and 4 GB of RAM, ~1.8 GB of which
  other services already use, and the collector must never be starved.
- **Decision:** Train on the VPS, set up like this:
  - Training code in its own uv workspace member (e.g. `model/` with its
    own `pyproject.toml`), importing `db_ml` for shared database code. The
    heavy ML dependencies stay out of the collector image.
  - Its own image and a `trainer` service in `compose.yaml` with a compose
    profile, so `docker compose up -d` doesn't start it; run on demand with
    `docker compose run --rm trainer`. CPU and memory limits (e.g. 1 CPU,
    1.5 GB) so training can't take the collector or the other services down.
  - Train from the nightly backup copy, not the live database: a long read
    transaction on the live database stops WAL checkpoints, so the WAL file
    grows while training runs.
- **Consequences:** Where the prediction API runs is still open (D20).

## D20. Where the prediction API runs
- **Date:** 2026-10-20, decided 2026-10-21
- **Status:** Accepted
- **Context:** The frontend will be hosted elsewhere, not on the VPS (D19).
  The prediction API needs the trained model and recent delays from the
  collector's database.
- **Earlier leaning:** on the VPS, as its own compose service behind HTTPS.
- **Decision:** For now the API runs locally, in a Docker container on the
  laptop. We decide where it runs in the cloud once there's enough data
  and the model has been tuned (D22).
- **Consequences:**
  - The model is trained on the VPS (D19) and copied to the laptop.
  - Its features need recent delays (7- and 28-day windows, D22), so the
    API reads a database copy brought over from the VPS, e.g. the
    nightly backup copy (D24). Predictions are only as fresh as that copy.
  - If the API fetches tomorrow's `/plan` itself, it shares the DB API
    rate limit with the collector, which already uses ~57 of its 60
    requests a minute. That needs a plan before the API makes live calls.

## D22. What the model predicts, and from which features
- **Date:** 2026-10-21
- **Status:** Open (target decided; features are hypotheses until EDA)
- **Decided:** The model answers "how late will the train I'm taking
  tomorrow be?", a day or more ahead. Predicting a train already running
  (live delay) is out of scope: no value in it for us.
- **Decided: delay bands, not one number.** The target is the arrival at
  the station where you get off, as one of: on time (under 6 min, DB's
  definition), 6–15, 15–30, 30–60, 60+ minutes, or cancelled. A
  classifier gives a probability per band, from which P(late) and an
  expected delay follow.
- **Why bands:** a day ahead, most of what makes a train late (a fault, a
  signal failure, someone on the tracks) hasn't happened yet, so even
  with perfect features the outcome stays spread out: usually on time,
  sometimes very late. A single number, the conditional mean, describes
  that badly and is pulled around by rare long delays. Bands also give
  cancellation a place. To be checked in EDA: if a plain regression's
  errors turn out narrow, revisit.
- **Consequence:** Every feature must be known a day ahead. In training,
  features for a stop are computed only from data before a cutoff (e.g.
  24 h before its planned time), so the model never sees what it couldn't
  know then.
- **Features:** the hypotheses, and whether we collect what each needs,
  are kept in [features.md](features.md). We test them with EDA once
  there's enough data (about four weeks).
- **Note:** D21 is the station2vec experiment, on its own branch.

## D23. Store the attributes the feature list needs (migration 3)
- **Date:** 2026-10-21
- **Status:** Accepted
- **Context:** Checking the feature hypotheses (D22,
  [features.md](features.md)) against the real XML showed four attributes
  DB sends that we didn't store. DB doesn't keep them, so every day
  without them is lost.
- **Decision:** Migration 3 adds:
  - `stops.operator` (`tl o`), e.g. `80` for DB Fernverkehr (F2).
  - When a cancellation was announced (`clt`), as `arrival_cancelled_at`
    and `departure_cancelled_at` on `stops` and `stop_changes` (F24). This
    tells cancellations known a day ahead from same-day ones.
  - A diverted train's changed path (`cpth`), as `changed_route_before`
    and `changed_route_after` on `stops` and `stop_changes` (F25). Empty
    text means DB sent it empty, which it does for cancelled events.
  - When a disruption notice applies (`from`, `to`) and its priority
    (`pr`), as `valid_from`, `valid_to` and `priority` on `stop_messages`
    (F23). Construction notices carry these, e.g. 14–25 Oct 2026.
- **Consequences:** Rows from before the migration have NULL in the new
  columns. Cancellation time and changed path are part of the snapshot, so
  after the deploy, stops that have them get one extra `stop_changes` row
  the next time they're seen. Messages are still stored once per stop
  (`INSERT OR IGNORE`), so a notice whose validity DB later extends keeps
  its first dates on that stop; new stops get the new dates.

## D24. Nightly backup to a Hetzner Storage Box with Borg
- **Date:** 2026-10-21
- **Status:** Accepted (running on the VPS since 2026-10-21)
- **Context:** The database exists only on the VPS disk. A consistent copy
  of a SQLite database in WAL mode can't be taken by copying the file while
  the collector writes. Training should read a copy, not the live file
  (D19).
- **Decision:**
  - Every night at 03:30 (VPS local time), a root systemd timer runs
    `deploy/backup/db-ml-backup.sh`: SQLite's online backup (`.backup`,
    which is consistent while the collector writes) into
    `/var/lib/db-ml-backup/db_ml.sqlite`, then `PRAGMA integrity_check`.
    Only a copy that passes replaces the previous one. That copy is the
    file training reads (D19).
  - The copy goes into a BorgBackup repository on the Storage Box, over
    SSH on port 23, with zstd compression. Borg deduplicates, so a full
    copy every night only uploads what changed; that's why it's `.backup`,
    which keeps SQLite's page layout, and not `VACUUM INTO`, which
    rewrites every page.
  - Kept: 14 daily, 8 weekly and 12 monthly archives.
  - Not encrypted: the data is public timetable data, and a lost
    passphrase would make every backup useless.
  - SQLite runs as the collector's user (10001), so it never leaves
    root-owned files next to the live database.
  - `db-ml-backup-check` is the restore test: it extracts the newest
    archive to a temporary folder, checks its integrity and prints row
    counts.
- **Why Borg over plain rsync of dated files:** keeping weeks of history
  costs little more than one copy, and it does compression, retention and
  checks itself. Hetzner supports it on Storage Boxes.
- **Consequences:** Setting it up needs root on the VPS and the Storage
  Box password once (`deploy/backup/install.sh`), so the user runs it.
  The Storage Box needs SSH support and external reachability turned on,
  even though the VPS is at Hetzner too (found during setup).
  Failures show in `systemctl --failed` and the journal; nothing alerts
  yet.
